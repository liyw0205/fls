import contextlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

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
