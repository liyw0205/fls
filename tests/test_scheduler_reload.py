import contextlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "scheduler-reload-token"


@contextlib.contextmanager
def isolated_app():
    keys = ("FLS_BASE_DIR", "FLS_TOKEN", "FLS_SECRET_KEY", "PYTHONDONTWRITEBYTECODE")
    old_env = {key: os.environ.get(key) for key in keys}

    with tempfile.TemporaryDirectory(prefix="fls-scheduler-reload-") as temp_dir:
        os.environ["FLS_BASE_DIR"] = temp_dir
        os.environ["FLS_TOKEN"] = TOKEN
        os.environ["FLS_SECRET_KEY"] = "scheduler-reload-secret"
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

        purge_fls_modules()
        try:
            if str(ROOT) not in sys.path:
                sys.path.insert(0, str(ROOT))
            from fls_manager.app import create_app
            yield create_app(), Path(temp_dir)
        finally:
            shutdown_scheduler()
            purge_fls_modules()
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def purge_fls_modules():
    for name in list(sys.modules):
        if name == "fls_manager" or name.startswith("fls_manager."):
            sys.modules.pop(name, None)


def shutdown_scheduler():
    state = sys.modules.get("fls_manager.state")
    scheduler = getattr(state, "scheduler", None) if state else None
    if scheduler is not None:
        with contextlib.suppress(Exception):
            scheduler.shutdown(wait=False)


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def task(task_id="t1", **updates):
    item = {
        "id": task_id,
        "name": f"任务 {task_id}",
        "command": "echo scheduler",
        "cron": "",
        "enabled": True,
        "collection_id": "",
        "run_count": 0,
        "pinned": False,
    }
    item.update(updates)
    return item


class SchedulerReloadTests(unittest.TestCase):
    def test_invalid_plan_preserves_jobs_and_records_failure(self):
        with isolated_app() as (_app, _base_dir):
            from fls_manager import paths, scheduler

            write_json(paths.TASK_FILE, [task(cron="* *")])
            old_job = SimpleNamespace(
                id="task-old",
                func=lambda: None,
                trigger=object(),
                args=[],
                kwargs={},
                name="old",
                misfire_grace_time=None,
                coalesce=True,
                max_instances=1,
            )
            fake = SimpleNamespace(
                get_jobs=lambda: [old_job],
                remove_all_jobs=lambda: (_ for _ in ()).throw(AssertionError("must not mutate")),
            )
            with patch.object(scheduler, "scheduler", fake):
                with self.assertRaises(scheduler.SchedulerReloadError) as caught:
                    scheduler.reload_scheduler()

            self.assertIn("调度计划校验失败", str(caught.exception))
            state = json.loads(paths.SCHEDULER_STATE_FILE.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["failed_tasks"][0]["task_id"], "t1")

    def test_apply_failure_restores_previous_jobs(self):
        with isolated_app() as (_app, _base_dir):
            from fls_manager import paths, scheduler

            write_json(paths.TASK_FILE, [task(cron="0 0 * * *")])
            old_job = SimpleNamespace(
                id="task-old",
                func=lambda: None,
                trigger=object(),
                args=[],
                kwargs={},
                name="old",
                misfire_grace_time=None,
                coalesce=True,
                max_instances=1,
            )
            added = []
            remove_calls = []

            def add_job(func, **kwargs):
                added.append(kwargs.get("id"))
                if kwargs.get("id") == "log_cleanup":
                    raise RuntimeError("cleanup scheduler unavailable")

            fake = SimpleNamespace(
                get_jobs=lambda: [old_job],
                remove_all_jobs=lambda: remove_calls.append(True),
                add_job=add_job,
            )
            with patch.object(scheduler, "scheduler", fake):
                with self.assertRaises(scheduler.SchedulerReloadError) as caught:
                    scheduler.reload_scheduler()

            self.assertIn("已恢复上一组调度", str(caught.exception))
            self.assertGreaterEqual(len(remove_calls), 2)
            self.assertIn("task-old", added)
            state = json.loads(paths.SCHEDULER_STATE_FILE.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "failed")
            self.assertIn("cleanup scheduler unavailable", state["error"])

    def test_reload_is_serialized_and_api_exposes_result(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager import scheduler

            active = 0
            peak = 0
            guard = threading.Lock()

            def apply(_plan):
                nonlocal active, peak
                with guard:
                    active += 1
                    peak = max(peak, active)
                time.sleep(0.03)
                with guard:
                    active -= 1

            with patch.object(scheduler, "_apply_scheduler_plan", side_effect=apply), \
                    patch.object(scheduler, "_record_scheduler_state", return_value={}):
                threads = [threading.Thread(target=scheduler.reload_scheduler) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=2)

            self.assertEqual(peak, 1)
            with patch("fls_manager.routes.api.get_scheduler_state", return_value={
                "status": "succeeded",
                "status_label": "已完成",
                "task_count": 1,
                "scheduled_count": 1,
                "failed_tasks": [],
                "error": "",
                "recovery": "",
                "updated_at": "now",
            }):
                response = app.test_client().get(
                    "/api/scheduler/status",
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["status"], "succeeded")

    def test_status_stays_running_during_same_process_reload(self):
        with isolated_app() as (_app, _base_dir):
            from fls_manager import scheduler

            entered = threading.Event()
            release = threading.Event()

            def apply(_plan):
                entered.set()
                self.assertTrue(release.wait(timeout=2))

            with patch.object(scheduler, "_apply_scheduler_plan", side_effect=apply):
                worker = threading.Thread(target=scheduler.reload_scheduler)
                worker.start()
                self.assertTrue(entered.wait(timeout=2))
                self.assertEqual(scheduler.get_scheduler_state()["status"], "running")
                release.set()
                worker.join(timeout=2)

            self.assertFalse(worker.is_alive())

    def test_running_snapshot_becomes_unknown_after_restart(self):
        with isolated_app() as (_app, _base_dir):
            from fls_manager import paths, scheduler

            write_json(paths.SCHEDULER_STATE_FILE, {
                "status": "running",
                "task_count": 3,
                "scheduled_count": 2,
                "failed_tasks": [],
                "error": "",
                "recovery": "",
                "updated_at": "before-restart",
            })

            state = scheduler.get_scheduler_state()

            self.assertEqual(state["status"], "unknown")
            self.assertIn("面板重启", state["error"])
            persisted = json.loads(paths.SCHEDULER_STATE_FILE.read_text(encoding="utf-8"))
            self.assertEqual(persisted["status"], "unknown")
            self.assertEqual(persisted["task_count"], 3)

    def test_api_reload_failure_returns_state_and_500(self):
        with isolated_app() as (app, _base_dir):
            with patch("fls_manager.routes.api.reload_scheduler", side_effect=RuntimeError("reload failed")), \
                    patch("fls_manager.routes.api.get_scheduler_state", return_value={
                        "status": "failed",
                        "status_label": "失败",
                        "task_count": 1,
                        "scheduled_count": 0,
                        "failed_tasks": [],
                        "error": "reload failed",
                        "recovery": "retry",
                        "updated_at": "now",
                    }):
                response = app.test_client().post(
                    "/api/scheduler/reload",
                    headers={"X-Token": TOKEN},
                )

            self.assertEqual(response.status_code, 500)
            payload = response.get_json()
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["state"]["error"], "reload failed")

    def test_dashboard_reload_reports_failure_and_keeps_task_data(self):
        with isolated_app() as (app, base_dir):
            from fls_manager import paths

            original = task("t1", cron="0 0 * * *")
            write_json(paths.TASK_FILE, [original])
            with patch("fls_manager.routes.dashboard.reload_scheduler", side_effect=RuntimeError("scheduler down")):
                response = app.test_client().post(
                    "/scheduler/reload",
                    headers={"X-Token": TOKEN},
                )

            self.assertEqual(response.status_code, 302)
            self.assertIn("err=", response.headers["Location"])
            self.assertEqual(json.loads(paths.TASK_FILE.read_text(encoding="utf-8"))[0]["id"], "t1")
            with patch("fls_manager.routes.dashboard.get_scheduler_state", return_value={
                "status": "failed",
                "status_label": "失败",
                "task_count": 1,
                "scheduled_count": 0,
                "failed_tasks": [],
                "error": "scheduler down",
                "recovery": "retry",
                "updated_at": "now",
            }):
                page = app.test_client().get("/", headers={"X-Token": TOKEN})
            self.assertEqual(page.status_code, 200)
            self.assertIn("调度器状态", page.get_data(as_text=True))
            self.assertIn("scheduler down", page.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
