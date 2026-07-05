import contextlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "bulk-workflow-token"


@contextlib.contextmanager
def isolated_app():
    keys = ("FLS_BASE_DIR", "FLS_TOKEN", "FLS_SECRET_KEY", "PYTHONDONTWRITEBYTECODE")
    old_env = {key: os.environ.get(key) for key in keys}

    with tempfile.TemporaryDirectory(prefix="fls-bulk-workflow-") as temp_dir:
        os.environ["FLS_BASE_DIR"] = temp_dir
        os.environ["FLS_TOKEN"] = TOKEN
        os.environ["FLS_SECRET_KEY"] = "bulk-workflow-secret"
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


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sample_task(task_id, **updates):
    task = {
        "id": task_id,
        "name": f"Task {task_id}",
        "command": f"task {task_id}.py",
        "cron": "",
        "collection_id": "",
        "enabled": True,
        "notify": {"mode": "none", "ids": []},
        "random_delay": {"mode": "none", "seconds": 0},
        "retry": {"attempts": 0, "interval_seconds": 60},
        "run_count": 0,
        "pinned": False,
        "created_at": "2026-07-04 00:00:00",
        "updated_at": "2026-07-04 00:00:00",
    }
    task.update(updates)
    return task


class BulkWorkflowTests(unittest.TestCase):
    def test_task_copy_resets_runtime_fields_and_keeps_retry_config(self):
        with isolated_app() as (app, base_dir):
            from fls_manager import paths

            write_json(
                paths.TASK_FILE,
                [
                    sample_task(
                        "t1",
                        name="Demo",
                        retry={"attempts": 2, "interval_seconds": 30},
                        run_count=7,
                        pinned=True,
                        last_run_at="2026-07-04 01:00:00",
                    )
                ],
            )

            with patch("fls_manager.routes.api.reload_scheduler") as reload_scheduler:
                response = app.test_client().post(
                    "/api/task/action/copy/t1",
                    headers={"X-Token": TOKEN},
                )

            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertTrue(payload["ok"])
            reload_scheduler.assert_called_once()

            tasks = read_json(base_dir / "data" / "tasks.json")
            self.assertEqual(len(tasks), 2)

            copied = [task for task in tasks if task["id"] != "t1"][0]
            self.assertEqual(copied["name"], "Demo-copy")
            self.assertEqual(copied["run_count"], 0)
            self.assertFalse(copied["pinned"])
            self.assertNotIn("last_run_at", copied)
            self.assertEqual(copied["retry"], {"attempts": 2, "interval_seconds": 30})

    def test_task_bulk_disable_clear_collection_and_delete(self):
        with isolated_app() as (app, base_dir):
            from fls_manager import paths

            write_json(
                paths.TASK_FILE,
                [
                    sample_task("t1", collection_id="c1"),
                    sample_task("t2", collection_id="c1"),
                    sample_task("t3", collection_id="c1"),
                ],
            )

            client = app.test_client()

            with patch("fls_manager.routes.api.reload_scheduler") as reload_scheduler:
                response = client.post(
                    "/api/task/bulk-action",
                    json={"action": "disable", "task_ids": ["t1", "t1", "t2"]},
                    headers={"X-Token": TOKEN},
                )

            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["action"], "disable")
            self.assertEqual(payload["count"], 2)
            self.assertEqual(payload["updated_count"], 2)
            reload_scheduler.assert_called_once()

            tasks = {task["id"]: task for task in read_json(base_dir / "data" / "tasks.json")}
            self.assertFalse(tasks["t1"]["enabled"])
            self.assertFalse(tasks["t2"]["enabled"])
            self.assertTrue(tasks["t3"]["enabled"])

            response = client.post(
                "/api/task/bulk-action",
                json={"action": "clear_collection", "task_ids": ["t1", "t2"]},
                headers={"X-Token": TOKEN},
            )

            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["action"], "clear_collection")
            self.assertEqual(payload["count"], 2)
            self.assertEqual(payload["updated_count"], 2)

            tasks = {task["id"]: task for task in read_json(base_dir / "data" / "tasks.json")}
            self.assertEqual(tasks["t1"]["collection_id"], "")
            self.assertEqual(tasks["t2"]["collection_id"], "")
            self.assertEqual(tasks["t3"]["collection_id"], "c1")

            with patch(
                "fls_manager.routes.api.stop_task_now",
                return_value=(True, "已结束"),
            ) as stop_task_now:
                with patch("fls_manager.routes.api.reload_scheduler") as reload_scheduler:
                    response = client.post(
                        "/api/task/bulk-action",
                        json={"action": "delete", "task_ids": ["t1", "t2"]},
                        headers={"X-Token": TOKEN},
                    )

            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["action"], "delete")
            self.assertEqual(payload["count"], 2)
            self.assertEqual(payload["deleted_count"], 2)
            self.assertEqual(payload["failed_count"], 0)
            self.assertEqual(payload["failures"], [])
            self.assertEqual(stop_task_now.call_count, 2)
            reload_scheduler.assert_called_once()
            self.assertEqual(
                [task["id"] for task in read_json(base_dir / "data" / "tasks.json")],
                ["t3"],
            )

    def test_task_bulk_delete_keeps_tasks_when_stop_fails(self):
        with isolated_app() as (app, base_dir):
            from fls_manager import paths

            write_json(
                paths.TASK_FILE,
                [
                    sample_task("t1"),
                    sample_task("t2"),
                    sample_task("t3"),
                ],
            )

            def fake_stop_task(task_id):
                results = {
                    "t1": (True, "已结束"),
                    "t2": (False, "停止失败"),
                    "t3": (False, "任务未运行"),
                }
                return results[task_id]

            with patch(
                "fls_manager.routes.api.stop_task_now",
                side_effect=fake_stop_task,
            ) as stop_task_now:
                with patch("fls_manager.routes.api.reload_scheduler") as reload_scheduler:
                    response = app.test_client().post(
                        "/api/task/bulk-action",
                        json={"action": "delete", "task_ids": ["t1", "t2", "t3"]},
                        headers={"X-Token": TOKEN},
                    )

            payload = response.get_json()

            self.assertEqual(response.status_code, 200)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["action"], "delete")
            self.assertEqual(payload["count"], 3)
            self.assertEqual(payload["deleted_count"], 2)
            self.assertEqual(payload["failed_count"], 1)
            self.assertEqual(payload["failures"], ["Task t2: 停止失败"])
            self.assertEqual(stop_task_now.call_count, 3)
            reload_scheduler.assert_called_once()
            self.assertEqual(
                [task["id"] for task in read_json(base_dir / "data" / "tasks.json")],
                ["t2"],
            )

    def test_task_bulk_run_and_stop_return_structured_status_fields(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager import paths

            write_json(
                paths.TASK_FILE,
                [
                    sample_task("t1"),
                    sample_task("t2"),
                    sample_task("t3"),
                ],
            )

            client = app.test_client()

            def fake_run_task(task_id, source="manual"):
                results = {
                    "t1": (True, "已提交启动"),
                    "t2": (False, "任务已在运行"),
                    "t3": (False, "命令为空"),
                }
                return results[task_id]

            with patch(
                "fls_manager.routes.api.run_task_now",
                side_effect=fake_run_task,
            ) as run_task_now:
                response = client.post(
                    "/api/task/bulk-action",
                    json={"action": "run", "task_ids": ["t1", "t2", "t3"]},
                    headers={"X-Token": TOKEN},
                )

            payload = response.get_json()

            self.assertEqual(response.status_code, 200)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["action"], "run")
            self.assertEqual(payload["count"], 3)
            self.assertEqual(payload["submitted_count"], 1)
            self.assertEqual(payload["failed_count"], 2)
            self.assertEqual(
                payload["failures"],
                ["Task t2: 任务已在运行", "Task t3: 命令为空"],
            )
            self.assertEqual(run_task_now.call_count, 3)

            def fake_stop_task(task_id):
                results = {
                    "t1": (True, "已结束"),
                    "t2": (False, "任务未运行"),
                    "t3": (False, "停止失败"),
                }
                return results[task_id]

            with patch(
                "fls_manager.routes.api.stop_task_now",
                side_effect=fake_stop_task,
            ) as stop_task_now:
                response = client.post(
                    "/api/task/bulk-action",
                    json={"action": "stop", "task_ids": ["t1", "t2", "t3"]},
                    headers={"X-Token": TOKEN},
                )

            payload = response.get_json()

            self.assertEqual(response.status_code, 200)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["action"], "stop")
            self.assertEqual(payload["count"], 3)
            self.assertEqual(payload["stopped_count"], 1)
            self.assertEqual(payload["skipped_count"], 1)
            self.assertEqual(payload["failed_count"], 1)
            self.assertEqual(payload["failures"], ["Task t3: 停止失败"])
            self.assertEqual(stop_task_now.call_count, 3)

    def test_task_bulk_rejects_empty_selection(self):
        with isolated_app() as (app, _base_dir):
            response = app.test_client().post(
                "/api/task/bulk-action",
                json={"action": "enable", "task_ids": []},
                headers={"X-Token": TOKEN},
            )

            self.assertEqual(response.status_code, 400)
            payload = response.get_json()
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["action"], "enable")
            self.assertEqual(payload["count"], 0)

    def test_task_action_run_missing_task_returns_404(self):
        with isolated_app() as (app, base_dir):
            from fls_manager import paths

            write_json(
                paths.TASK_FILE,
                [
                    sample_task("t1"),
                ],
            )

            response = app.test_client().post(
                "/api/task/action/run/missing",
                headers={"X-Token": TOKEN},
            )

            payload = response.get_json()

            self.assertEqual(response.status_code, 404)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["msg"], "任务不存在")
            self.assertEqual(
                [task["id"] for task in read_json(base_dir / "data" / "tasks.json")],
                ["t1"],
            )

    def test_legacy_run_route_post_submits_task_and_uses_safe_back(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager import paths

            write_json(
                paths.TASK_FILE,
                [
                    sample_task("t1"),
                ],
            )

            with patch(
                "fls_manager.routes.tasks.actions.run_task_now",
                return_value=(True, "已提交启动"),
            ) as run_task_now:
                response = app.test_client().post(
                    "/run/t1?back=/tasks",
                    headers={"X-Token": TOKEN},
                )

            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers.get("Location"), "/log/t1?back=/tasks")
            run_task_now.assert_called_once_with("t1", source="manual")

    def test_legacy_run_route_missing_task_returns_404_without_write(self):
        with isolated_app() as (app, base_dir):
            from fls_manager import paths

            write_json(
                paths.TASK_FILE,
                [
                    sample_task("t1"),
                ],
            )

            with patch(
                "fls_manager.routes.tasks.actions.run_task_now",
                return_value=(False, "任务不存在"),
            ) as run_task_now:
                response = app.test_client().post(
                    "/run/missing?back=/tasks",
                    headers={"X-Token": TOKEN},
                )

            self.assertEqual(response.status_code, 404)
            self.assertIn("任务不存在", response.get_data(as_text=True))
            run_task_now.assert_called_once_with("missing", source="manual")
            self.assertEqual(
                [task["id"] for task in read_json(base_dir / "data" / "tasks.json")],
                ["t1"],
            )

    def test_task_action_stop_missing_task_returns_404_without_stop_call(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager import paths

            write_json(
                paths.TASK_FILE,
                [
                    sample_task("t1"),
                ],
            )

            with patch("fls_manager.routes.api.stop_task_now") as stop_task_now:
                response = app.test_client().post(
                    "/api/task/action/stop/missing",
                    headers={"X-Token": TOKEN},
                )

            payload = response.get_json()

            self.assertEqual(response.status_code, 404)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["msg"], "任务不存在")
            stop_task_now.assert_not_called()

    def test_task_action_stop_existing_not_running_keeps_200_failure(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager import paths

            write_json(
                paths.TASK_FILE,
                [
                    sample_task("t1"),
                ],
            )

            with patch(
                "fls_manager.routes.api.stop_task_now",
                return_value=(False, "任务未运行"),
            ) as stop_task_now:
                response = app.test_client().post(
                    "/api/task/action/stop/t1",
                    headers={"X-Token": TOKEN},
                )

            payload = response.get_json()

            self.assertEqual(response.status_code, 200)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["msg"], "任务未运行")
            stop_task_now.assert_called_once_with("t1")

    def test_legacy_stop_route_existing_task_redirects_when_not_running(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager import paths

            write_json(
                paths.TASK_FILE,
                [
                    sample_task("t1"),
                ],
            )

            with patch(
                "fls_manager.routes.tasks.actions.stop_task_now",
                return_value=(False, "任务未运行"),
            ) as stop_task_now:
                response = app.test_client().post(
                    "/stop/t1?back=/tasks",
                    headers={"X-Token": TOKEN},
                )

            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers.get("Location"), "/tasks")
            stop_task_now.assert_called_once_with("t1")

    def test_legacy_stop_route_missing_task_aborts_without_stop_call(self):
        with isolated_app() as (app, base_dir):
            from fls_manager import paths

            write_json(
                paths.TASK_FILE,
                [
                    sample_task("t1"),
                ],
            )

            with patch("fls_manager.routes.tasks.actions.stop_task_now") as stop_task_now:
                response = app.test_client().post(
                    "/stop/missing?back=/tasks",
                    headers={"X-Token": TOKEN},
                )

            self.assertEqual(response.status_code, 404)
            stop_task_now.assert_not_called()
            self.assertEqual(
                [task["id"] for task in read_json(base_dir / "data" / "tasks.json")],
                ["t1"],
            )

    def test_task_action_delete_existing_task_stops_removes_and_reloads(self):
        with isolated_app() as (app, base_dir):
            from fls_manager import paths

            write_json(
                paths.TASK_FILE,
                [
                    sample_task("t1"),
                    sample_task("t2"),
                ],
            )

            with patch(
                "fls_manager.routes.api.stop_task_now",
                return_value=(True, "已结束"),
            ) as stop_task_now:
                with patch("fls_manager.routes.api.reload_scheduler") as reload_scheduler:
                    response = app.test_client().post(
                        "/api/task/action/delete/t1",
                        headers={"X-Token": TOKEN},
                    )

            payload = response.get_json()

            self.assertEqual(response.status_code, 200)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["msg"], "已删除")
            stop_task_now.assert_called_once_with("t1")
            reload_scheduler.assert_called_once()
            self.assertEqual(
                [task["id"] for task in read_json(base_dir / "data" / "tasks.json")],
                ["t2"],
            )

    def test_task_action_delete_not_running_still_removes_task(self):
        with isolated_app() as (app, base_dir):
            from fls_manager import paths

            write_json(
                paths.TASK_FILE,
                [
                    sample_task("t1"),
                    sample_task("t2"),
                ],
            )

            with patch(
                "fls_manager.routes.api.stop_task_now",
                return_value=(False, "任务未运行"),
            ) as stop_task_now:
                with patch("fls_manager.routes.api.reload_scheduler") as reload_scheduler:
                    response = app.test_client().post(
                        "/api/task/action/delete/t1",
                        headers={"X-Token": TOKEN},
                    )

            payload = response.get_json()

            self.assertEqual(response.status_code, 200)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["msg"], "已删除")
            stop_task_now.assert_called_once_with("t1")
            reload_scheduler.assert_called_once()
            self.assertEqual(
                [task["id"] for task in read_json(base_dir / "data" / "tasks.json")],
                ["t2"],
            )

    def test_task_action_delete_stop_failure_keeps_task_without_reload(self):
        with isolated_app() as (app, base_dir):
            from fls_manager import paths

            write_json(
                paths.TASK_FILE,
                [
                    sample_task("t1"),
                    sample_task("t2"),
                ],
            )

            with patch(
                "fls_manager.routes.api.stop_task_now",
                return_value=(False, "结束失败：permission denied"),
            ) as stop_task_now:
                with patch("fls_manager.routes.api.save_tasks") as save_tasks:
                    with patch("fls_manager.routes.api.reload_scheduler") as reload_scheduler:
                        response = app.test_client().post(
                            "/api/task/action/delete/t1",
                            headers={"X-Token": TOKEN},
                        )

            payload = response.get_json()

            self.assertEqual(response.status_code, 409)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["msg"], "删除失败：结束失败：permission denied")
            stop_task_now.assert_called_once_with("t1")
            save_tasks.assert_not_called()
            reload_scheduler.assert_not_called()
            self.assertEqual(
                [task["id"] for task in read_json(base_dir / "data" / "tasks.json")],
                ["t1", "t2"],
            )

    def test_task_action_delete_missing_task_returns_404_without_side_effects(self):
        with isolated_app() as (app, base_dir):
            from fls_manager import paths

            write_json(
                paths.TASK_FILE,
                [
                    sample_task("t1"),
                    sample_task("t2"),
                ],
            )

            with patch("fls_manager.routes.api.stop_task_now") as stop_task_now:
                with patch("fls_manager.routes.api.save_tasks") as save_tasks:
                    with patch("fls_manager.routes.api.reload_scheduler") as reload_scheduler:
                        response = app.test_client().post(
                            "/api/task/action/delete/missing",
                            headers={"X-Token": TOKEN},
                        )

            payload = response.get_json()

            self.assertEqual(response.status_code, 404)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["msg"], "任务不存在")
            stop_task_now.assert_not_called()
            save_tasks.assert_not_called()
            reload_scheduler.assert_not_called()
            self.assertEqual(
                [task["id"] for task in read_json(base_dir / "data" / "tasks.json")],
                ["t1", "t2"],
            )

    def test_legacy_task_delete_stop_failure_keeps_task_without_reload(self):
        with isolated_app() as (app, base_dir):
            from fls_manager import paths

            write_json(
                paths.TASK_FILE,
                [
                    sample_task("t1"),
                    sample_task("t2"),
                ],
            )

            with patch(
                "fls_manager.routes.tasks.actions.stop_task_now",
                return_value=(False, '结束失败：<bad & "x">'),
            ) as stop_task_now:
                with patch("fls_manager.routes.tasks.actions.save_tasks") as save_tasks:
                    with patch("fls_manager.routes.tasks.actions.reload_scheduler") as reload_scheduler:
                        response = app.test_client().post(
                            "/task/delete/t1?back=https://example.invalid/out",
                            headers={"X-Token": TOKEN},
                        )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 409)
            self.assertIn('<div class="card-title">删除失败</div>', html)
            self.assertIn('style="color:#dc2626;font-weight:800;"', html)
            self.assertIn("删除失败：结束失败：&lt;bad &amp; &quot;x&quot;&gt;", html)
            self.assertIn('href="/tasks"', html)
            self.assertNotIn("<bad", html)
            stop_task_now.assert_called_once_with("t1")
            save_tasks.assert_not_called()
            reload_scheduler.assert_not_called()
            self.assertEqual(
                [task["id"] for task in read_json(base_dir / "data" / "tasks.json")],
                ["t1", "t2"],
            )

    def test_legacy_task_toggle_post_updates_task_and_uses_safe_back(self):
        with isolated_app() as (app, base_dir):
            from fls_manager import paths

            write_json(
                paths.TASK_FILE,
                [
                    sample_task("t1", enabled=True),
                    sample_task("t2", enabled=True),
                ],
            )

            with patch("fls_manager.routes.tasks.actions.reload_scheduler") as reload_scheduler:
                response = app.test_client().post(
                    "/task/toggle/t1?back=/tasks",
                    headers={"X-Token": TOKEN},
                )

            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers.get("Location"), "/tasks")
            reload_scheduler.assert_called_once()

            tasks = {task["id"]: task for task in read_json(base_dir / "data" / "tasks.json")}
            self.assertFalse(tasks["t1"]["enabled"])
            self.assertTrue(tasks["t2"]["enabled"])

    def test_legacy_task_toggle_missing_task_aborts_without_side_effects(self):
        with isolated_app() as (app, base_dir):
            from fls_manager import paths

            write_json(
                paths.TASK_FILE,
                [
                    sample_task("t1", enabled=True),
                    sample_task("t2", enabled=False),
                ],
            )

            with patch("fls_manager.routes.tasks.actions.save_tasks") as save_tasks:
                with patch("fls_manager.routes.tasks.actions.reload_scheduler") as reload_scheduler:
                    response = app.test_client().post(
                        "/task/toggle/missing?back=/tasks",
                        headers={"X-Token": TOKEN},
                    )

            self.assertEqual(response.status_code, 404)
            save_tasks.assert_not_called()
            reload_scheduler.assert_not_called()

            tasks = {task["id"]: task for task in read_json(base_dir / "data" / "tasks.json")}
            self.assertTrue(tasks["t1"]["enabled"])
            self.assertFalse(tasks["t2"]["enabled"])

    def test_legacy_task_pin_post_updates_task_and_uses_safe_back(self):
        with isolated_app() as (app, base_dir):
            from fls_manager import paths

            write_json(
                paths.TASK_FILE,
                [
                    sample_task("t1", pinned=False),
                    sample_task("t2", pinned=False),
                ],
            )

            response = app.test_client().post(
                "/task/pin/t1?back=https://example.invalid/out",
                headers={"X-Token": TOKEN},
            )

            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers.get("Location"), "/tasks")

            tasks = {task["id"]: task for task in read_json(base_dir / "data" / "tasks.json")}
            self.assertTrue(tasks["t1"]["pinned"])
            self.assertFalse(tasks["t2"]["pinned"])

    def test_legacy_task_pin_missing_task_aborts_without_write(self):
        with isolated_app() as (app, base_dir):
            from fls_manager import paths

            write_json(
                paths.TASK_FILE,
                [
                    sample_task("t1", pinned=False),
                    sample_task("t2", pinned=True),
                ],
            )

            with patch("fls_manager.routes.tasks.actions.save_tasks") as save_tasks:
                response = app.test_client().post(
                    "/task/pin/missing?back=/tasks",
                    headers={"X-Token": TOKEN},
                )

            self.assertEqual(response.status_code, 404)
            save_tasks.assert_not_called()

            tasks = {task["id"]: task for task in read_json(base_dir / "data" / "tasks.json")}
            self.assertFalse(tasks["t1"]["pinned"])
            self.assertTrue(tasks["t2"]["pinned"])

    def test_legacy_task_pin_limit_renders_error_card_without_write(self):
        with isolated_app() as (app, base_dir):
            from fls_manager import paths

            write_json(
                paths.TASK_FILE,
                [
                    sample_task(f"t{i}", pinned=True)
                    for i in range(1, 6)
                ] + [
                    sample_task("t6", pinned=False),
                ],
            )

            with patch("fls_manager.routes.tasks.actions.save_tasks") as save_tasks:
                response = app.test_client().post(
                    "/task/pin/t6?back=/collections",
                    headers={"X-Token": TOKEN},
                )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 400)
            self.assertIn('<div class="card-title">置顶失败</div>', html)
            self.assertIn('style="color:#dc2626;font-weight:800;"', html)
            self.assertIn("最多只能置顶 5 个任务", html)
            self.assertIn('href="/collections"', html)
            save_tasks.assert_not_called()

            tasks = {task["id"]: task for task in read_json(base_dir / "data" / "tasks.json")}
            self.assertFalse(tasks["t6"]["pinned"])

    def test_collection_add_task_accepts_multiple_task_ids(self):
        with isolated_app() as (app, base_dir):
            from fls_manager import paths

            write_json(
                paths.COLLECTION_FILE,
                [
                    {
                        "id": "c1",
                        "name": "合集一",
                        "remark": "",
                        "created_at": "2026-07-04 00:00:00",
                        "updated_at": "2026-07-04 00:00:00",
                    }
                ],
            )
            write_json(
                paths.TASK_FILE,
                [
                    sample_task("t1"),
                    sample_task("t2"),
                    sample_task("t3"),
                ],
            )

            client = app.test_client()
            response = client.post(
                "/collection/add-task/c1",
                data={"task_ids": ["t1", "t2"]},
                headers={"X-Token": TOKEN},
            )

            self.assertEqual(response.status_code, 302)

            tasks = {task["id"]: task for task in read_json(base_dir / "data" / "tasks.json")}
            self.assertEqual(tasks["t1"]["collection_id"], "c1")
            self.assertEqual(tasks["t2"]["collection_id"], "c1")
            self.assertEqual(tasks["t3"]["collection_id"], "")

            response = client.get("/collections", headers={"X-Token": TOKEN})
            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('name="task_ids"', html)
            self.assertIn("multiple", html)
            self.assertIn("collection-bulk-toolbar", html)
            self.assertIn("flsCollectionTaskBulkAction", html)
            self.assertIn("flsBulkActionMessage(json", html)

    def test_collection_add_task_keeps_legacy_task_id_compatibility_and_dedupes(self):
        with isolated_app() as (app, base_dir):
            from fls_manager import paths

            write_json(
                paths.COLLECTION_FILE,
                [
                    {
                        "id": "c1",
                        "name": "合集一",
                        "remark": "",
                        "created_at": "2026-07-04 00:00:00",
                        "updated_at": "2026-07-04 00:00:00",
                    }
                ],
            )
            write_json(
                paths.TASK_FILE,
                [
                    sample_task("t1"),
                    sample_task("t2"),
                    sample_task("t3"),
                ],
            )

            response = app.test_client().post(
                "/collection/add-task/c1",
                data={
                    "task_ids": ["t1", "t1", "t2"],
                    "task_id": "t2",
                },
                headers={"X-Token": TOKEN},
            )

            self.assertEqual(response.status_code, 302)

            tasks = {task["id"]: task for task in read_json(base_dir / "data" / "tasks.json")}
            self.assertEqual(tasks["t1"]["collection_id"], "c1")
            self.assertEqual(tasks["t2"]["collection_id"], "c1")
            self.assertEqual(tasks["t3"]["collection_id"], "")

            write_json(
                paths.TASK_FILE,
                [
                    sample_task("t1"),
                    sample_task("t2"),
                ],
            )

            response = app.test_client().post(
                "/collection/add-task/c1",
                data={"task_id": "t1"},
                headers={"X-Token": TOKEN},
            )

            self.assertEqual(response.status_code, 302)

            tasks = {task["id"]: task for task in read_json(base_dir / "data" / "tasks.json")}
            self.assertEqual(tasks["t1"]["collection_id"], "c1")
            self.assertEqual(tasks["t2"]["collection_id"], "")

    def test_collection_add_task_missing_task_aborts_without_partial_write(self):
        with isolated_app() as (app, base_dir):
            from fls_manager import paths

            write_json(
                paths.COLLECTION_FILE,
                [
                    {
                        "id": "c1",
                        "name": "合集一",
                        "remark": "",
                        "created_at": "2026-07-04 00:00:00",
                        "updated_at": "2026-07-04 00:00:00",
                    }
                ],
            )
            original_tasks = [
                sample_task("t1"),
                sample_task("t2"),
            ]
            write_json(paths.TASK_FILE, original_tasks)

            response = app.test_client().post(
                "/collection/add-task/c1",
                data={"task_ids": ["t1", "missing"]},
                headers={"X-Token": TOKEN},
            )

            self.assertEqual(response.status_code, 404)
            tasks = {task["id"]: task for task in read_json(base_dir / "data" / "tasks.json")}
            self.assertEqual(tasks["t1"]["collection_id"], "")
            self.assertEqual(tasks["t2"]["collection_id"], "")

    def test_collection_delete_empty_collection_skips_task_write(self):
        with isolated_app() as (app, base_dir):
            from fls_manager import paths

            write_json(
                paths.COLLECTION_FILE,
                [
                    {
                        "id": "c1",
                        "name": "空合集",
                        "remark": "",
                        "created_at": "2026-07-04 00:00:00",
                        "updated_at": "2026-07-04 00:00:00",
                    }
                ],
            )
            write_json(
                paths.TASK_FILE,
                [
                    sample_task("t1"),
                    sample_task("t2"),
                ],
            )

            with patch("fls_manager.routes.tasks.collections.save_tasks") as save_tasks:
                response = app.test_client().post(
                    "/collection/delete/c1?back=/collections",
                    headers={"X-Token": TOKEN},
                )

            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers.get("Location"), "/collections")
            save_tasks.assert_not_called()
            self.assertEqual(read_json(base_dir / "data" / "collections.json"), [])
            self.assertEqual(
                [task["id"] for task in read_json(base_dir / "data" / "tasks.json")],
                ["t1", "t2"],
            )

    def test_collection_delete_clears_member_tasks(self):
        with isolated_app() as (app, base_dir):
            from fls_manager import paths

            write_json(
                paths.COLLECTION_FILE,
                [
                    {
                        "id": "c1",
                        "name": "合集一",
                        "remark": "",
                        "created_at": "2026-07-04 00:00:00",
                        "updated_at": "2026-07-04 00:00:00",
                    }
                ],
            )
            write_json(
                paths.TASK_FILE,
                [
                    sample_task("t1", collection_id="c1"),
                    sample_task("t2"),
                ],
            )

            response = app.test_client().post(
                "/collection/delete/c1?back=/collections",
                headers={"X-Token": TOKEN},
            )

            self.assertEqual(response.status_code, 302)
            self.assertEqual(read_json(base_dir / "data" / "collections.json"), [])

            tasks = {task["id"]: task for task in read_json(base_dir / "data" / "tasks.json")}
            self.assertEqual(tasks["t1"]["collection_id"], "")
            self.assertEqual(tasks["t2"]["collection_id"], "")

    def test_collection_delete_missing_collection_aborts_without_writes(self):
        with isolated_app() as (app, base_dir):
            from fls_manager import paths

            collections = [
                {
                    "id": "c1",
                    "name": "合集一",
                    "remark": "",
                    "created_at": "2026-07-04 00:00:00",
                    "updated_at": "2026-07-04 00:00:00",
                }
            ]
            write_json(paths.COLLECTION_FILE, collections)
            write_json(paths.TASK_FILE, [sample_task("t1", collection_id="c1")])

            with patch("fls_manager.routes.tasks.collections.save_collections") as save_collections:
                with patch("fls_manager.routes.tasks.collections.save_tasks") as save_tasks:
                    response = app.test_client().post(
                        "/collection/delete/missing?back=/collections",
                        headers={"X-Token": TOKEN},
                    )

            self.assertEqual(response.status_code, 404)
            save_collections.assert_not_called()
            save_tasks.assert_not_called()
            self.assertEqual(read_json(base_dir / "data" / "collections.json"), collections)
            self.assertEqual(
                [task["collection_id"] for task in read_json(base_dir / "data" / "tasks.json")],
                ["c1"],
            )

    def test_task_collection_clear_existing_member_writes_task_file(self):
        with isolated_app() as (app, base_dir):
            from fls_manager import paths

            write_json(
                paths.TASK_FILE,
                [
                    sample_task("t1", collection_id="c1"),
                    sample_task("t2"),
                ],
            )

            response = app.test_client().post(
                "/task/collection/clear/t1?back=/collections",
                headers={"X-Token": TOKEN},
            )

            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers.get("Location"), "/collections")

            tasks = {task["id"]: task for task in read_json(base_dir / "data" / "tasks.json")}
            self.assertEqual(tasks["t1"]["collection_id"], "")
            self.assertEqual(tasks["t2"]["collection_id"], "")

    def test_task_collection_clear_unassigned_task_skips_write(self):
        with isolated_app() as (app, base_dir):
            from fls_manager import paths

            write_json(
                paths.TASK_FILE,
                [
                    sample_task("t1"),
                    sample_task("t2"),
                ],
            )

            with patch("fls_manager.routes.tasks.actions.save_tasks") as save_tasks:
                response = app.test_client().post(
                    "/task/collection/clear/t1?back=/collections",
                    headers={"X-Token": TOKEN},
                )

            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers.get("Location"), "/collections")
            save_tasks.assert_not_called()
            self.assertEqual(
                [task["id"] for task in read_json(base_dir / "data" / "tasks.json")],
                ["t1", "t2"],
            )

    def test_task_collection_clear_missing_task_aborts_without_write(self):
        with isolated_app() as (app, base_dir):
            from fls_manager import paths

            write_json(
                paths.TASK_FILE,
                [
                    sample_task("t1", collection_id="c1"),
                ],
            )

            with patch("fls_manager.routes.tasks.actions.save_tasks") as save_tasks:
                response = app.test_client().post(
                    "/task/collection/clear/missing?back=/collections",
                    headers={"X-Token": TOKEN},
                )

            self.assertEqual(response.status_code, 404)
            save_tasks.assert_not_called()
            self.assertEqual(
                [task["collection_id"] for task in read_json(base_dir / "data" / "tasks.json")],
                ["c1"],
            )

    def test_collection_task_cards_keep_post_actions_collapsed_command_and_anchor_back(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager import paths

            write_json(
                paths.COLLECTION_FILE,
                [
                    {
                        "id": "c1",
                        "name": "合集一",
                        "remark": "",
                        "created_at": "2026-07-04 00:00:00",
                        "updated_at": "2026-07-04 00:00:00",
                    }
                ],
            )
            write_json(
                paths.TASK_FILE,
                [
                    sample_task(
                        "t1",
                        name="合集任务",
                        command="task demo.py " + "--flag " * 24,
                        collection_id="c1",
                        config_path="conf/app.yml",
                    ),
                    sample_task("t2", name="可加入任务"),
                ],
            )

            response = app.test_client().get(
                "/collections?task_q=available",
                headers={"X-Token": TOKEN},
            )
            html = response.get_data(as_text=True)

            encoded_collection_back = (
                "/collections%3Ftask_q%3Davailable%23collection-c1"
            )

            self.assertEqual(response.status_code, 200)
            self.assertIn("fls-collapsible-code", html)
            self.assertIn("fls-value-preview", html)

            self.assertIn(
                f'href="/task/config/t1?back={encoded_collection_back}"',
                html,
            )
            self.assertIn(
                f'href="/task/edit/t1?back={encoded_collection_back}"',
                html,
            )
            self.assertIn(
                f'action="/stop/t1?back={encoded_collection_back}"',
                html,
            )
            self.assertIn(
                f'action="/run/t1?back={encoded_collection_back}"',
                html,
            )
            self.assertIn(
                f'action="/task/pin/t1?back={encoded_collection_back}"',
                html,
            )
            self.assertIn(
                f'action="/task/collection/clear/t1?back={encoded_collection_back}"',
                html,
            )
            self.assertIn(
                'action="/collection/add-task/c1?back='
                '/collections%3Ftask_q%3Davailable%23collection-c1"',
                html,
            )
            self.assertIn(
                'action="/collection/delete/c1?back='
                '/collections%3Ftask_q%3Davailable"',
                html,
            )

            self.assertNotIn('href="/run/t1', html)
            self.assertNotIn('href="/stop/t1', html)
            self.assertNotIn('href="/task/pin/t1', html)
            self.assertNotIn('href="/task/collection/clear/t1', html)
            self.assertNotIn('href="/collection/delete/c1', html)

    def test_log_groups_delete_deletes_only_selected_groups(self):
        with isolated_app() as (app, base_dir):
            log_dir = base_dir / "log"
            log_dir.mkdir(parents=True, exist_ok=True)

            alpha_one = log_dir / "alpha-one.log"
            alpha_two = log_dir / "alpha-two.log"
            beta = log_dir / "beta.log"

            alpha_one.write_text("===== 启动任务: Alpha =====\n", encoding="utf-8")
            alpha_two.write_text("===== 启动任务: Alpha =====\n", encoding="utf-8")
            beta.write_text("===== 启动任务: Beta =====\n", encoding="utf-8")

            response = app.test_client().post(
                "/api/logs/groups/delete",
                json={"groups": ["Alpha"]},
                headers={"X-Token": TOKEN},
            )

            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.get_json()["ok"])
            self.assertEqual(response.get_json()["deleted"], 2)
            self.assertFalse(alpha_one.exists())
            self.assertFalse(alpha_two.exists())
            self.assertTrue(beta.exists())

    def test_log_groups_delete_requires_selection(self):
        with isolated_app() as (app, _base_dir):
            response = app.test_client().post(
                "/api/logs/groups/delete",
                json={"groups": []},
                headers={"X-Token": TOKEN},
            )

            self.assertEqual(response.status_code, 400)
            self.assertFalse(response.get_json()["ok"])


if __name__ == "__main__":
    unittest.main()
