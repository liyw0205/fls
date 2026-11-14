import json
import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

try:
    from tests.test_ui_route_components import TOKEN, isolated_app
except ModuleNotFoundError:
    from test_ui_route_components import TOKEN, isolated_app


class ScriptDebugRecoveryTests(unittest.TestCase):
    def test_real_start_and_watcher_persist_terminal_success(self):
        with isolated_app() as (app, base_dir):
            script = base_dir / "scripts" / "demo.py"
            script.write_text("print('debug ok')\n", encoding="utf-8")
            from fls_manager.routes.scripts import debug

            with patch.object(
                debug,
                "debug_command_info",
                return_value={
                    "cmd": [sys.executable, "-c", "print('debug ok')"],
                    "display_cmd": "python -c ...",
                    "cwd": str(base_dir),
                    "shell": False,
                },
            ):
                response = app.test_client().get("/scripts/debug/demo.py", headers={"X-Token": TOKEN})

            self.assertEqual(response.status_code, 302)
            debug_id = urlsplit(response.headers["Location"]).path.rsplit("/", 1)[-1]
            deadline = time.monotonic() + 3
            record = None
            while time.monotonic() < deadline:
                record = debug.get_script_debug_run(debug_id)
                if record and record["status"] == "succeeded":
                    break
                time.sleep(0.02)

            self.assertIsNotNone(record)
            self.assertEqual(record["status"], "succeeded")
            self.assertFalse(record["running"])
            self.assertEqual(record["returncode"], 0)
            self.assertTrue(Path(record["log_file"]).exists())

    def test_start_exception_is_durable_failed_result(self):
        with isolated_app() as (app, base_dir):
            script = base_dir / "scripts" / "broken.py"
            script.write_text("raise SystemExit(1)\n", encoding="utf-8")
            from fls_manager.routes.scripts import debug

            with patch.object(
                debug,
                "debug_command_info",
                return_value={
                    "cmd": [sys.executable, str(script)],
                    "display_cmd": "broken",
                    "cwd": str(base_dir),
                    "shell": False,
                },
            ), patch.object(debug.subprocess, "Popen", side_effect=OSError("spawn denied")):
                response = app.test_client().get("/scripts/debug/broken.py", headers={"X-Token": TOKEN})

            self.assertEqual(response.status_code, 302)
            debug_id = urlsplit(response.headers["Location"]).path.rsplit("/", 1)[-1]
            record = debug.get_script_debug_run(debug_id)
            self.assertEqual(record["status"], "failed")
            self.assertFalse(record["running"])
            self.assertEqual(record["returncode"], -1)
            self.assertIn("spawn denied", record["error"])

    def test_stop_failure_persists_retryable_running_state(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.routes.scripts import debug

            debug_id = "retry-stop"
            info = {
                "id": debug_id,
                "script": "/tmp/demo.py",
                "rel": "demo.py",
                "log_file": str(base_dir / "log" / "retry-stop.log"),
                "running": True,
                "process": SimpleNamespace(pid=33, poll=Mock(return_value=None)),
                "pid": 33,
                "returncode": None,
                "error": "",
                "created_at": "2026-11-15 00:00:00",
            }
            debug.SCRIPT_DEBUG_RUNNING[debug_id] = info
            with patch.object(debug, "terminate_debug_process", return_value=False):
                response = app.test_client().post(
                    f"/scripts/debug-stop/{debug_id}", headers={"X-Token": TOKEN}
                )

            self.assertEqual(response.status_code, 409)
            persisted = debug.get_script_debug_run(debug_id)
            self.assertEqual(persisted["status"], "running")
            self.assertTrue(persisted["running"])
            self.assertIn("停止失败", persisted["error"])

    def test_manual_stop_persists_stopped_without_watcher_overwrite(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.routes.scripts import debug

            debug_id = "manual-stop"
            log_file = base_dir / "log" / "manual-stop.log"
            proc = SimpleNamespace(pid=44, poll=Mock(side_effect=[None, -15]))
            debug.SCRIPT_DEBUG_RUNNING[debug_id] = {
                "id": debug_id,
                "script": "/tmp/demo.py",
                "rel": "demo.py",
                "log_file": str(log_file),
                "running": True,
                "process": proc,
                "pid": 44,
                "returncode": None,
                "error": "",
                "created_at": "2026-11-15 00:00:00",
            }
            with patch.object(debug, "terminate_debug_process", return_value=True):
                response = app.test_client().post(
                    f"/scripts/debug-stop/{debug_id}", headers={"X-Token": TOKEN}
                )

            self.assertEqual(response.status_code, 302)
            persisted = debug.get_script_debug_run(debug_id)
            self.assertEqual(persisted["status"], "stopped")
            self.assertEqual(persisted["returncode"], -15)
            self.assertEqual(persisted["error"], "用户手动停止")

    def test_watcher_failure_persists_failed_result(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.routes.scripts import debug

            debug_id = "watch-fail"
            log_file = base_dir / "log" / "watch-fail.log"
            log_file.touch()
            proc = SimpleNamespace(wait=Mock(return_value=9))
            debug.SCRIPT_DEBUG_RUNNING[debug_id] = {
                "id": debug_id,
                "script": "/tmp/demo.py",
                "rel": "demo.py",
                "log_file": str(log_file),
                "running": True,
                "process": proc,
                "pid": 55,
                "returncode": None,
                "error": "",
                "created_at": "2026-11-15 00:00:00",
            }
            with open(log_file, "ab", buffering=0) as log_fp:
                debug.script_debug_watch(debug_id, proc, log_fp)

            persisted = debug.get_script_debug_run(debug_id)
            self.assertEqual(persisted["status"], "failed")
            self.assertEqual(persisted["returncode"], 9)
            self.assertIn("退出码", persisted["error"])

    def test_restart_reconciles_inflight_once_and_page_api_match(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.paths import SCRIPT_DEBUG_RUNS_FILE
            from fls_manager.routes.scripts import debug

            row = {
                "id": "restart-1",
                "script": "/tmp/demo.py",
                "rel": "demo.py",
                "log_file": str(base_dir / "log" / "restart-1.log"),
                "pid": 901,
                "status": "running",
                "running": True,
                "returncode": None,
                "error": "原始诊断",
                "created_at": "2026-11-15 00:00:00",
                "updated_at": "2026-11-15 00:00:01",
            }
            SCRIPT_DEBUG_RUNS_FILE.write_text(json.dumps([row], ensure_ascii=False), encoding="utf-8")
            first = debug.load_script_debug_runs()
            second = debug.load_script_debug_runs()

            self.assertEqual(first, second)
            self.assertEqual(first[0]["status"], "unknown")
            self.assertIn("原始诊断", first[0]["error"])
            self.assertEqual(first[0]["pid"], 901)

            client = app.test_client()
            api = client.get("/api/scripts/debug-log/restart-1", headers={"X-Token": TOKEN})
            page = client.get("/scripts/debug-log/restart-1", headers={"X-Token": TOKEN})
            self.assertEqual(api.status_code, 200)
            self.assertEqual(page.status_code, 200)
            payload = api.get_json()
            self.assertEqual(payload["status"], "unknown")
            self.assertFalse(payload["running"])
            self.assertIn("结果未知", page.get_data(as_text=True))
            self.assertNotIn("停止调试运行", page.get_data(as_text=True))

    def test_missing_debug_id_is_404_for_page_and_api(self):
        with isolated_app() as (app, _base_dir):
            client = app.test_client()
            self.assertEqual(
                client.get("/scripts/debug-log/missing", headers={"X-Token": TOKEN}).status_code,
                404,
            )
            response = client.get("/api/scripts/debug-log/missing", headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.get_json()["msg"], "调试记录不存在")

    def test_malformed_and_overlong_history_is_bounded(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.paths import SCRIPT_DEBUG_RUNS_FILE
            from fls_manager.routes.scripts import debug

            SCRIPT_DEBUG_RUNS_FILE.write_text(
                json.dumps(
                    [{
                        "id": "long-error",
                        "status": "failed",
                        "error": "x" * 30000,
                    }, None, {"status": "failed"}],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            rows = debug.load_script_debug_runs()
            self.assertEqual(len(rows), 1)
            self.assertEqual(len(rows[0]["error"]), debug.SCRIPT_DEBUG_TEXT_LIMIT)
            persisted = json.loads(SCRIPT_DEBUG_RUNS_FILE.read_text(encoding="utf-8"))
            self.assertEqual(len(persisted), 1)
            self.assertLessEqual(len(persisted[0]["error"]), debug.SCRIPT_DEBUG_TEXT_LIMIT)

            SCRIPT_DEBUG_RUNS_FILE.write_text("{not-json", encoding="utf-8")
            self.assertEqual(debug.load_script_debug_runs(), [])
            self.assertEqual(json.loads(SCRIPT_DEBUG_RUNS_FILE.read_text(encoding="utf-8")), [])


if __name__ == "__main__":
    unittest.main()
