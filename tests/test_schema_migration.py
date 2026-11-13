import contextlib
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]


@contextlib.contextmanager
def isolated_fls_modules():
    keys = ("FLS_BASE_DIR", "PYTHONDONTWRITEBYTECODE")
    old_env = {key: os.environ.get(key) for key in keys}

    with tempfile.TemporaryDirectory(prefix="fls-schema-test-") as temp_dir:
        os.environ["FLS_BASE_DIR"] = temp_dir
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        purge_fls_modules()

        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))

        try:
            yield Path(temp_dir)
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


class SchemaMigrationTests(unittest.TestCase):
    def test_load_tasks_migrates_legacy_and_bad_fields(self):
        with isolated_fls_modules():
            from fls_manager import models, paths

            write_json(
                paths.TASK_FILE,
                [
                    {
                        "id": " task-1 ",
                        "name": " Demo ",
                        "command": " task demo.py ",
                        "enabled": "0",
                        "env": {
                            " TOKEN ": 123,
                            "": "skip",
                            "EMPTY": None,
                        },
                        "notify_ids": ["__default__"],
                        "random_delay": {
                            "mode": "custom",
                            "seconds": "999",
                        },
                        "retry_count": "99",
                        "run_count": "bad",
                        "pinned": "true",
                        "remark": None,
                        "collection_id": None,
                    },
                    {
                        "command": " echo fallback ",
                        "notify_ids": ["n1", "n1", ""],
                        "random_delay": {
                            "mode": "bad",
                            "seconds": 5,
                        },
                        "enabled": "yes",
                    },
                    "bad row",
                ],
            )

            tasks = models.load_tasks()

            self.assertEqual(len(tasks), 2)

            first = tasks[0]
            self.assertEqual(first["id"], "task-1")
            self.assertEqual(first["name"], "Demo")
            self.assertEqual(first["command"], "task demo.py")
            self.assertFalse(first["enabled"])
            self.assertEqual(first["env"], {"TOKEN": "123", "EMPTY": ""})
            self.assertEqual(first["notify"], {"mode": "default", "ids": []})
            self.assertNotIn("notify_ids", first)
            self.assertEqual(
                first["random_delay"],
                {"mode": "custom", "seconds": 120},
            )
            self.assertEqual(
                first["retry"],
                {"attempts": 5, "interval_seconds": 60},
            )
            self.assertNotIn("retry_count", first)
            self.assertEqual(first["run_count"], 0)
            self.assertTrue(first["pinned"])
            self.assertEqual(first["remark"], "")
            self.assertEqual(first["collection_id"], "")

            second = tasks[1]
            self.assertTrue(second["id"])
            self.assertEqual(second["name"], "echo fallback")
            self.assertEqual(
                second["notify"],
                {"mode": "custom", "ids": ["n1"]},
            )
            self.assertEqual(
                second["random_delay"],
                {"mode": "none", "seconds": 0},
            )
            self.assertEqual(
                second["retry"],
                {"attempts": 0, "interval_seconds": 60},
            )
            self.assertTrue(second["enabled"])

            direct = models.normalize_task(
                {
                    "id": "direct",
                    "notify": {
                        "mode": "default",
                        "ids": ["should-drop"],
                    },
                    "random_delay": {
                        "mode": "default",
                        "seconds": 88,
                    },
                    "retry": {
                        "attempts": "3",
                        "interval_seconds": "30",
                    },
                }
            )
            self.assertEqual(direct["notify"], {"mode": "default", "ids": []})
            self.assertEqual(
                direct["random_delay"],
                {"mode": "default", "seconds": 0},
            )
            self.assertEqual(
                direct["retry"],
                {"attempts": 3, "interval_seconds": 30},
            )

            self.assertEqual(read_json(paths.TASK_FILE), tasks)

    def test_task_history_helpers_filter_update_prepend_and_trim_records(self):
        with isolated_fls_modules():
            from fls_manager import models, paths

            models.TASK_HISTORY_LIMIT = 3
            write_json(
                paths.TASK_HISTORY_FILE,
                [
                    {
                        "id": "h1",
                        "task_id": "t1",
                        "status": "success",
                    },
                    "bad row",
                    {
                        "id": "h2",
                        "task_id": "t2",
                        "status": "failed",
                    },
                    {
                        "id": "h3",
                        "task_id": "t1",
                        "status": "running",
                    },
                ],
            )

            history = models.load_task_history()

            self.assertEqual(
                [item["id"] for item in history],
                ["h1", "h2", "h3"],
            )
            self.assertEqual(
                [item["id"] for item in models.task_history_for_task("t1", 1)],
                ["h1"],
            )
            self.assertEqual(models.task_history_for_task("missing"), [])

            self.assertTrue(
                models.update_task_history(
                    "h2",
                    {
                        "status": "success",
                        "message": "fixed",
                    },
                )
            )
            self.assertFalse(models.update_task_history("", {"status": "failed"}))
            self.assertFalse(models.update_task_history("missing", {"status": "failed"}))

            persisted = read_json(paths.TASK_HISTORY_FILE)
            self.assertEqual(
                [item["id"] for item in persisted],
                ["h1", "h2", "h3"],
            )
            self.assertEqual(persisted[1]["status"], "success")
            self.assertEqual(persisted[1]["message"], "fixed")

            models.add_task_history(
                {
                    "id": "h0",
                    "task_id": "t0",
                    "status": "starting",
                }
            )

            trimmed = read_json(paths.TASK_HISTORY_FILE)

            self.assertEqual(
                [item["id"] for item in trimmed],
                ["h0", "h1", "h2"],
            )
            self.assertNotIn("h3", [item["id"] for item in trimmed])

    def test_task_history_restart_recovery_is_idempotent_and_preserves_terminal_rows(self):
        with isolated_fls_modules():
            from fls_manager import models, paths

            write_json(
                paths.TASK_HISTORY_FILE,
                [
                    {
                        "id": "h-running",
                        "status": "running",
                        "start_at": "2026-11-14 09:00:00",
                        "end_at": "",
                        "return_code": None,
                        "log_file": "/tmp/running.log",
                        "message": "进程已启动",
                    },
                    {
                        "id": "h-starting",
                        "status": "starting",
                        "end_at": "",
                        "message": "已提交启动",
                    },
                    {
                        "id": "h-delaying",
                        "status": "delaying",
                        "end_at": "",
                    },
                    {
                        "id": "h-success",
                        "status": "success",
                        "end_at": "",
                        "message": "运行成功",
                    },
                    {
                        "id": "h-failed",
                        "status": "failed",
                        "end_at": "",
                        "message": "退出码 7",
                    },
                    {
                        "id": "h-timeout",
                        "status": "timeout",
                        "end_at": "",
                        "message": "任务超时",
                    },
                    {
                        "id": "h-stopped",
                        "status": "stopped",
                        "end_at": "",
                        "message": "手动结束",
                    },
                    {
                        "id": "h-ended",
                        "status": "running",
                        "end_at": "2026-11-14 09:00:05",
                        "message": "已结束",
                    },
                    {
                        "id": "h-unknown",
                        "status": "unknown",
                        "end_at": "2026-11-14 09:00:06",
                        "message": "之前已恢复",
                    },
                ],
            )

            with patch.object(models, "now_str", return_value="2026-11-14 10:00:00"):
                self.assertEqual(models.reconcile_task_history_after_restart(), 3)

            recovered = {item["id"]: item for item in read_json(paths.TASK_HISTORY_FILE)}
            self.assertEqual(recovered["h-running"]["status"], "unknown")
            self.assertEqual(recovered["h-running"]["end_at"], "2026-11-14 10:00:00")
            self.assertEqual(recovered["h-running"]["return_code"], None)
            self.assertEqual(recovered["h-running"]["log_file"], "/tmp/running.log")
            self.assertIn("重启前状态：运行中", recovered["h-running"]["message"])
            self.assertIn("原说明：进程已启动", recovered["h-running"]["message"])
            self.assertEqual(recovered["h-starting"]["status"], "unknown")
            self.assertEqual(recovered["h-delaying"]["status"], "unknown")
            self.assertEqual(recovered["h-success"]["status"], "success")
            self.assertEqual(recovered["h-success"]["end_at"], "")
            self.assertEqual(recovered["h-failed"]["status"], "failed")
            self.assertEqual(recovered["h-timeout"]["status"], "timeout")
            self.assertEqual(recovered["h-stopped"]["status"], "stopped")
            self.assertEqual(recovered["h-ended"]["status"], "running")
            self.assertEqual(recovered["h-unknown"]["message"], "之前已恢复")

            first_write = read_json(paths.TASK_HISTORY_FILE)
            with patch.object(models, "now_str", return_value="2026-11-14 11:00:00"):
                self.assertEqual(models.reconcile_task_history_after_restart(), 0)
            self.assertEqual(read_json(paths.TASK_HISTORY_FILE), first_write)

            with patch.object(models, "now_str", return_value="2026-11-14 12:00:00"):
                self.assertEqual(
                    models.reconcile_task_history_after_restart({"h-running"}),
                    0,
                )
            self.assertEqual(read_json(paths.TASK_HISTORY_FILE), first_write)

    def test_create_app_reconciles_history_before_serving_requests(self):
        with isolated_fls_modules():
            from fls_manager import paths, models

            write_json(
                paths.TASK_HISTORY_FILE,
                [
                    {"id": "h1", "status": "running", "end_at": "", "message": "进行中"},
                    {"id": "h-active", "status": "running", "end_at": "", "message": "仍受控"},
                ],
            )

            from fls_manager import task_runner
            from fls_manager.app import create_app

            task_runner.RUNNING["active-task"] = {"history_id": "h-active"}
            with patch.object(models, "now_str", return_value="2026-11-14 12:00:00"):
                app = create_app()

            response = app.test_client().get("/history")
            self.assertEqual(response.status_code, 302)
            self.assertIn("/setup", response.headers.get("Location", ""))
            history = read_json(paths.TASK_HISTORY_FILE)
            by_id = {item["id"]: item for item in history}
            self.assertEqual(by_id["h1"]["status"], "unknown")
            self.assertEqual(by_id["h1"]["end_at"], "2026-11-14 12:00:00")
            self.assertEqual(by_id["h-active"]["status"], "running")
            self.assertEqual(by_id["h-active"]["end_at"], "")

    def test_task_history_concurrent_updates_keep_all_records(self):
        with isolated_fls_modules():
            from fls_manager import models, paths

            write_json(paths.TASK_HISTORY_FILE, [])
            barrier = threading.Barrier(8)
            errors = []

            def add_record(index):
                try:
                    barrier.wait(timeout=2)
                    models.add_task_history({
                        "id": f"concurrent-{index}",
                        "task_id": f"task-{index}",
                        "status": "starting",
                    })
                except BaseException as exc:
                    errors.append(exc)

            workers = [threading.Thread(target=add_record, args=(index,)) for index in range(8)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=3)
            self.assertTrue(all(not worker.is_alive() for worker in workers))
            self.assertEqual(errors, [])

            persisted = read_json(paths.TASK_HISTORY_FILE)
            self.assertEqual(
                {item["id"] for item in persisted},
                {f"concurrent-{index}" for index in range(8)},
            )

            update_barrier = threading.Barrier(8)
            errors = []

            def update_record(index):
                try:
                    update_barrier.wait(timeout=2)
                    if not models.update_task_history(
                        f"concurrent-{index}",
                        {"status": "success"},
                    ):
                        raise AssertionError(
                            f"history record concurrent-{index} was not updated"
                        )
                except BaseException as exc:
                    errors.append(exc)

            workers = [threading.Thread(target=update_record, args=(index,)) for index in range(8)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=3)
            self.assertTrue(all(not worker.is_alive() for worker in workers))
            self.assertEqual(errors, [])

            persisted = read_json(paths.TASK_HISTORY_FILE)
            self.assertEqual(len(persisted), 8)
            self.assertTrue(all(item["status"] == "success" for item in persisted))

    def test_load_global_env_normalizes_keys_and_values(self):
        with isolated_fls_modules():
            from fls_manager import models, paths

            write_json(
                paths.GLOBAL_ENV_FILE,
                {
                    " TOKEN ": 123,
                    "EMPTY": None,
                    "": "skip",
                },
            )

            env = models.load_global_env()

            self.assertEqual(env, {"TOKEN": "123", "EMPTY": ""})
            self.assertEqual(read_json(paths.GLOBAL_ENV_FILE), env)

    def test_load_proxies_normalizes_rows_and_persists(self):
        with isolated_fls_modules():
            from fls_manager import models, paths

            write_json(
                paths.PROXY_FILE,
                [
                    {
                        "name": "",
                        "type": "BAD",
                        "host": " 127.0.0.1 ",
                        "port": 1080,
                        "enabled": "false",
                        "created_at": None,
                    },
                    "bad row",
                ],
            )

            proxies = models.load_proxies()

            self.assertEqual(len(proxies), 1)
            proxy = proxies[0]
            self.assertTrue(proxy["id"])
            self.assertEqual(proxy["name"], "未命名代理")
            self.assertEqual(proxy["type"], "http")
            self.assertEqual(proxy["host"], "127.0.0.1")
            self.assertEqual(proxy["port"], "1080")
            self.assertFalse(proxy["enabled"])
            self.assertEqual(proxy["created_at"], "")
            self.assertEqual(read_json(paths.PROXY_FILE), proxies)

    def test_load_collections_normalizes_existing_partial_schema(self):
        with isolated_fls_modules():
            from fls_manager import models, paths

            write_json(
                paths.COLLECTION_FILE,
                [
                    {
                        "id": " c1 ",
                        "name": "",
                        "remark": 123,
                        "created_at": None,
                    },
                    {
                        "name": "missing id",
                    },
                ],
            )

            collections = models.load_collections()

            self.assertEqual(
                collections,
                [
                    {
                        "id": "c1",
                        "name": "未命名合集",
                        "remark": "123",
                        "created_at": "",
                        "updated_at": "",
                    }
                ],
            )
            self.assertEqual(read_json(paths.COLLECTION_FILE), collections)

    def test_load_config_clamps_core_values(self):
        with isolated_fls_modules():
            from fls_manager import config, paths
            from fls_manager.routes.about.helpers import (
                timezone_from_offset,
                utc_offset_options,
            )

            write_json(
                paths.CONFIG_FILE,
                {
                    "admin_token": " token ",
                    "security_verify_enabled": "yes",
                    "security_verify_type": "bad",
                    "totp_secret": None,
                    "port": "70000",
                    "online_script_source": "",
                    "log_cleanup_minutes": "0",
                    "log_max_size_mb": "bad",
                    "log_keep_per_task": "-1",
                    "task_timeout_seconds": "-10",
                    "random_delay_seconds": "999",
                    "timezone_offset_hours": "99",
                    "panel_time_offset_seconds": "bad",
                    "task_types": {
                        "py": "0",
                        "js": "1",
                        "unknown": True,
                    },
                },
            )

            cfg = config.load_config()

            self.assertEqual(cfg["admin_token"], "token")
            self.assertTrue(cfg["security_verify_enabled"])
            self.assertEqual(cfg["security_verify_type"], "code")
            self.assertEqual(cfg["totp_secret"], "")
            self.assertEqual(cfg["port"], 65535)
            self.assertEqual(
                cfg["online_script_source"],
                config.DEFAULT_CONFIG["online_script_source"],
            )
            self.assertEqual(cfg["log_cleanup_minutes"], 1)
            self.assertEqual(cfg["log_max_size_mb"], config.LOG_MAX_SIZE_MB)
            self.assertEqual(cfg["log_keep_per_task"], 1)
            self.assertEqual(cfg["task_timeout_seconds"], 0)
            self.assertEqual(cfg["random_delay_seconds"], 120)
            self.assertEqual(cfg["timezone_offset_hours"], 23)
            self.assertEqual(cfg["panel_time_offset_seconds"], 0)
            self.assertFalse(cfg["task_types"]["py"])
            self.assertTrue(cfg["task_types"]["js"])
            self.assertNotIn("unknown", cfg["task_types"])

            tz = config.get_panel_timezone()
            self.assertEqual(tz.utcoffset(None).total_seconds(), 23 * 3600)

            helper_tz = timezone_from_offset(99)
            self.assertEqual(helper_tz.utcoffset(None).total_seconds(), 23 * 3600)

            options = utc_offset_options(99)
            self.assertIn('value="23" selected', options)
            self.assertNotIn('value="24"', options)
            self.assertNotIn('value="-24"', options)


if __name__ == "__main__":
    unittest.main()
