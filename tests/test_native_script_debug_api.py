import json
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

try:
    from tests.test_ui_route_components import TOKEN, isolated_app
except ModuleNotFoundError:
    from test_ui_route_components import TOKEN, isolated_app


class NativeScriptDebugApiTests(unittest.TestCase):
    def _native_client(self, app, csrf=True):
        from fls_manager.native_sessions import create_native_session

        with app.app_context():
            session_id, csrf_token, _ = create_native_session(
                TOKEN, remember=True, verified=True,
            )
        client = app.test_client()
        client.set_cookie("fls_native_session", session_id)
        headers = {"X-CSRF-Token": csrf_token} if csrf else {}
        return client, headers

    def _start(self, client, headers, path="demo.py", request_id="a" * 32):
        return client.post(
            "/api/scripts/debug-runs",
            json={"path": path, "request_id": request_id},
            headers=headers,
        )

    def _wait_for_status(self, client, headers, debug_id, expected):
        deadline = time.monotonic() + 3
        latest = None
        while time.monotonic() < deadline:
            response = client.get(
                f"/api/scripts/debug-runs/{debug_id}",
                headers=headers,
            )
            latest = response.json.get("run")
            if latest and latest["status"] in expected:
                return response.json
            time.sleep(0.02)
        self.fail(f"debug run did not reach {expected}: {latest}")

    def test_get_history_is_read_only_and_native_writes_require_csrf(self):
        with isolated_app() as (app, base):
            (base / "scripts" / "demo.py").write_text("print('never started')\n", encoding="utf-8")
            import fls_manager.routes.scripts.debug as debug

            client = app.test_client()
            unauthorized = client.get("/api/scripts/debug-runs")
            self.assertEqual(unauthorized.status_code, 401)

            native, headers = self._native_client(app, csrf=False)
            missing_csrf = self._start(native, headers)
            self.assertEqual(missing_csrf.status_code, 400)

            native, headers = self._native_client(app)
            with patch.object(debug.subprocess, "Popen") as popen:
                history = native.get("/api/scripts/debug-runs?path=demo.py", headers=headers)
                self.assertEqual(history.status_code, 200)
                self.assertEqual(history.json["runs"], [])
                self.assertEqual(popen.call_count, 0)
                self.assertEqual(debug.SCRIPT_DEBUG_RUNNING, {})

    def test_start_replay_and_path_conflict_are_idempotent(self):
        with isolated_app() as (app, base):
            (base / "scripts" / "demo.py").write_text("print('debug ok')\n", encoding="utf-8")
            (base / "scripts" / "other.py").write_text("print('other')\n", encoding="utf-8")
            import fls_manager.routes.scripts.debug as debug

            client, headers = self._native_client(app)
            with patch.object(
                debug,
                "debug_command_info",
                return_value={
                    "cmd": [sys.executable, "-c", "print('debug ok')"],
                    "display_cmd": "python -c ...",
                    "cwd": str(base),
                    "shell": False,
                },
            ):
                first = self._start(client, headers)
                self.assertEqual(first.status_code, 202)
                debug_id = first.json["run"]["id"]
                self._wait_for_status(client, headers, debug_id, {"succeeded"})
                replay = self._start(client, headers)
                conflict = self._start(client, headers, path="other.py")

            self.assertEqual(replay.status_code, 200)
            self.assertTrue(replay.json["replayed"])
            self.assertEqual(replay.json["run"]["id"], debug_id)
            self.assertEqual(conflict.status_code, 409)
            self.assertEqual(conflict.json["code"], "request_id_conflict")
            self.assertEqual(len(debug.load_script_debug_runs()), 1)

    def test_log_and_error_payloads_hide_server_paths_and_secrets(self):
        with isolated_app() as (app, base):
            script = base / "scripts" / "demo.py"
            script.write_text("print('unused')\n", encoding="utf-8")
            import fls_manager.routes.scripts.debug as debug

            private_path = str(base / "private" / "service.log")
            client, headers = self._native_client(app)
            with patch.object(
                debug,
                "debug_command_info",
                return_value={
                    "cmd": [
                        sys.executable,
                        "-c",
                        f"print({private_path!r}); print('token=never-return-this')",
                    ],
                    "display_cmd": "private command",
                    "cwd": str(base),
                    "shell": False,
                },
            ):
                accepted = self._start(client, headers)
                debug_id = accepted.json["run"]["id"]
                detail = self._wait_for_status(client, headers, debug_id, {"succeeded"})

            payload = detail["run"]
            self.assertEqual(payload["path"], "demo.py")
            self.assertNotIn("script", payload)
            self.assertNotIn("pid", payload)
            serialized = json.dumps(detail, ensure_ascii=False)
            self.assertNotIn(str(base), serialized)
            self.assertNotIn("never-return-this", serialized)
            self.assertNotIn("private command", serialized)
            self.assertIn("<path>", detail["log"])

    def test_start_validation_and_persistence_failure_do_not_launch(self):
        with isolated_app() as (app, base):
            (base / "scripts" / "demo.py").write_text("print('unused')\n", encoding="utf-8")
            import fls_manager.routes.scripts.debug as debug

            client, headers = self._native_client(app)
            traversal = self._start(client, headers, path="../outside.py")
            missing = self._start(client, headers, path="missing.py", request_id="b" * 32)
            self.assertEqual(traversal.status_code, 400)
            self.assertEqual(missing.status_code, 404)

            with patch.object(debug, "persist_script_debug", side_effect=OSError("disk full")), patch.object(
                debug.subprocess, "Popen",
            ) as popen:
                failed = self._start(client, headers, request_id="c" * 32)

            self.assertEqual(failed.status_code, 503)
            self.assertEqual(failed.json["code"], "run_persistence_failed")
            self.assertEqual(popen.call_count, 0)
            self.assertEqual(debug.SCRIPT_DEBUG_RUNNING, {})

    def test_start_rejection_and_spawn_failure_are_safe_and_replayable(self):
        with isolated_app() as (app, base):
            (base / "scripts" / "demo.py").write_text("print('unused')\n", encoding="utf-8")
            import fls_manager.routes.scripts.debug as debug
            import fls_manager.routes.scripts.native_debug as native_debug

            client, headers = self._native_client(app)
            with patch.object(native_debug, "pending_script_rename", return_value={"id": "r"}), patch.object(
                debug.subprocess, "Popen",
            ) as popen:
                pending = self._start(client, headers)
            self.assertEqual(pending.status_code, 409)
            self.assertEqual(pending.json["code"], "rename_recovery_pending")
            popen.assert_not_called()

            private_path = str(base / "private" / "spawn.log")
            with patch.object(
                debug,
                "debug_command_info",
                return_value={"cmd": [sys.executable, "demo.py"], "cwd": str(base), "shell": False},
            ), patch.object(
                debug.subprocess,
                "Popen",
                side_effect=OSError(f"spawn denied at {private_path}: token=do-not-show"),
            ) as popen:
                failed = self._start(client, headers, request_id="b" * 32)
                debug_id = failed.json["run"]["id"]
                detail = client.get(f"/api/scripts/debug-runs/{debug_id}", headers=headers)
                replay = self._start(client, headers, request_id="b" * 32)

            self.assertEqual(failed.status_code, 202)
            self.assertEqual(failed.json["run"]["status"], "failed")
            self.assertEqual(detail.status_code, 200)
            self.assertEqual(replay.status_code, 200)
            self.assertTrue(replay.json["replayed"])
            self.assertEqual(popen.call_count, 1)
            serialized = json.dumps(detail.json, ensure_ascii=False)
            self.assertNotIn(str(base), serialized)
            self.assertNotIn("do-not-show", serialized)

    def test_json_and_query_bounds_are_enforced(self):
        with isolated_app() as (app, base):
            (base / "scripts" / "demo.py").write_text("print('unused')\n", encoding="utf-8")
            client, headers = self._native_client(app)
            import fls_manager.routes.scripts.native_debug as native_debug

            non_json = client.post(
                "/api/scripts/debug-runs",
                data="{}",
                content_type="text/plain",
                headers=headers,
            )
            oversized = client.post(
                "/api/scripts/debug-runs",
                data=json.dumps({"path": "demo.py", "request_id": "a" * 32, "x": "x" * (16 * 1024)}),
                content_type="application/json",
                headers=headers,
            )
            bad_history = client.get("/api/scripts/debug-runs?limit=0", headers=headers)
            with patch.object(
                native_debug,
                "_debug_current_record",
                return_value={"id": "a" * 32, "rel": "demo.py", "log_file": "", "status": "succeeded"},
            ):
                bad_log = client.get(f"/api/scripts/debug-runs/{'a' * 32}?lines=1201", headers=headers)

            self.assertEqual(non_json.status_code, 415)
            self.assertEqual(oversized.status_code, 413)
            self.assertEqual(bad_history.status_code, 400)
            self.assertEqual(bad_log.status_code, 400)

    def test_stop_is_explicit_and_unknown_runs_are_not_signalled(self):
        with isolated_app() as (app, base):
            import fls_manager.routes.scripts.debug as debug
            import fls_manager.routes.scripts.native_debug as native_debug

            client, headers = self._native_client(app)
            debug_id = "d" * 32
            log_file = base / "log" / "debug.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.touch()
            proc = SimpleNamespace(pid=44, poll=Mock(side_effect=[None, -15]))
            info = {
                "id": debug_id,
                "request_id": "e" * 32,
                "script": str(base / "scripts" / "demo.py"),
                "rel": "demo.py",
                "log_file": str(log_file),
                "running": True,
                "process": proc,
                "pid": 44,
                "returncode": None,
                "error": "",
                "status": "running",
                "created_at": "2026-11-26 00:00:00",
            }
            debug.SCRIPT_DEBUG_RUNNING[debug_id] = info
            debug.persist_script_debug(info)
            with patch.object(native_debug, "terminate_debug_process", return_value=True):
                stopped = client.post(
                    f"/api/scripts/debug-runs/{debug_id}/stop",
                    headers=headers,
                )
            self.assertEqual(stopped.status_code, 200)
            self.assertEqual(stopped.json["run"]["status"], "stopped")
            self.assertEqual(stopped.json["run"]["returncode"], -15)

            from fls_manager.paths import SCRIPT_DEBUG_RUNS_FILE

            debug.SCRIPT_DEBUG_RUNNING.pop(debug_id, None)
            SCRIPT_DEBUG_RUNS_FILE.write_text(
                json.dumps([{
                    "id": "f" * 32,
                    "status": "running",
                    "running": True,
                    "script": str(base / "scripts" / "demo.py"),
                    "rel": "demo.py",
                    "log_file": str(log_file),
                }]),
                encoding="utf-8",
            )
            unknown_id = "f" * 32
            with patch.object(native_debug, "terminate_debug_process") as terminate:
                response = client.post(
                    f"/api/scripts/debug-runs/{unknown_id}/stop",
                    headers=headers,
                )
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json["code"], "process_unavailable")
            terminate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
