import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.test_ui_route_components import TOKEN, isolated_app


class OnlineSourceRefreshTests(unittest.TestCase):
    def response(self, body="[]", status=200):
        response = Mock()
        response.status_code = status
        response.text = body
        response.headers = {"Content-Type": "application/json"}
        return response

    def test_source_response_is_closed_and_cache_publish_is_atomic(self):
        with isolated_app() as (_app, base_dir):
            from fls_manager.online_scripts import source

            response = self.response('[{"id":"demo","name":"Demo","type":"raw","link":"https://example.invalid/demo.sh"}]')
            with patch.object(source, "requests", SimpleNamespace(get=Mock(return_value=response))), patch.object(
                source, "github_proxy_url", side_effect=lambda url, *_args: url,
            ):
                items = source.fetch_online_scripts()
            self.assertEqual(items[0]["id"], "demo")
            response.close.assert_called_once()
            cache = base_dir / "data" / "online_scripts_cache.json"
            self.assertEqual(json.loads(cache.read_text())[0]["id"], "demo")

            failed = self.response("not-json")
            with patch.object(source, "requests", SimpleNamespace(get=Mock(return_value=failed))), patch.object(
                source, "github_proxy_url", side_effect=lambda url, *_args: url,
            ):
                with self.assertRaisesRegex(RuntimeError, "不是合法 JSON"):
                    source.fetch_online_scripts()
            failed.close.assert_called_once()
            self.assertEqual(json.loads(cache.read_text())[0]["id"], "demo")

    def test_task_link_response_is_closed_and_error_does_not_discard_main_source(self):
        with isolated_app() as (_app, base_dir):
            from fls_manager.online_scripts import source

            main = self.response('[{"id":"demo","name":"Demo","type":"raw","link":"https://example.invalid/demo.sh","task_link":"https://example.invalid/tasks.json"}]')
            task = self.response("bad json")
            with patch.object(source, "requests", SimpleNamespace(get=Mock(side_effect=[main, task]))), patch.object(
                source, "github_proxy_url", side_effect=lambda url, *_args: url,
            ):
                items = source.fetch_online_scripts()
            self.assertEqual(items[0]["id"], "demo")
            main.close.assert_called_once()
            task.close.assert_called_once()
            self.assertEqual(json.loads((base_dir / "data" / "online_scripts_cache.json").read_text())[0]["id"], "demo")

    def test_refresh_worker_persists_success_and_failure_without_losing_cache(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import source
            from fls_manager.online_scripts.constants import ONLINE_REFRESH_STATE_FILE, get_online_refresh_state

            source.save_online_script_cache([{"id": "old", "name": "Old", "type": "raw", "link": "x"}])
            with patch.object(source, "fetch_online_scripts", return_value=[{"id": "new"}]):
                source.refresh_worker("r-success", log_file=str(base_dir / "success.log"))
            self.assertEqual(get_online_refresh_state()["status"], "已完成")
            self.assertEqual(json.loads((base_dir / "data" / "online_scripts_cache.json").read_text())[0]["id"], "new")
            self.assertEqual(json.loads(ONLINE_REFRESH_STATE_FILE.read_text())["id"], "r-success")

            source.save_online_script_cache([{"id": "stable", "name": "Stable", "type": "raw", "link": "x"}])
            with patch.object(source, "fetch_online_scripts", side_effect=TimeoutError("upstream timeout")):
                source.refresh_worker("r-failure", log_file=str(base_dir / "failure.log"))
            state = get_online_refresh_state()
            self.assertEqual(state["status"], "失败")
            self.assertIn("upstream timeout", state["error"])
            self.assertEqual(json.loads((base_dir / "data" / "online_scripts_cache.json").read_text())[0]["id"], "stable")
            payload = app.test_client().get("/api/online-scripts/refresh-status", headers={"X-Token": TOKEN}).json
            self.assertEqual(payload["status"], "失败")

    def test_api_refresh_serializes_concurrent_starts_and_reports_start_failure(self):
        with isolated_app() as (app, _base_dir):
            import fls_manager.routes.online_scripts.refresh as refresh

            gate = threading.Event()
            started = threading.Event()
            completed = threading.Event()

            def blocked(refresh_id, *_args, **_kwargs):
                started.set()
                def finish():
                    gate.wait(timeout=3)
                    refresh.update_online_refresh_state(
                        id=refresh_id,
                        running=False,
                        status="已完成",
                        message="同步在线脚本源成功，共 0 条",
                        error="",
                        updated_at="2026-11-14 00:00:00",
                        log_file="",
                    )
                    completed.set()

                threading.Thread(target=finish, daemon=True).start()

            with patch.object(refresh, "start_refresh_thread", side_effect=blocked):
                first = app.test_client().post("/api/online-scripts/refresh", headers={"X-Token": TOKEN})
                self.assertEqual(first.status_code, 202)
                self.assertTrue(started.wait(1))
                second = app.test_client().post("/api/online-scripts/refresh", headers={"X-Token": TOKEN})
                self.assertEqual(second.status_code, 409)
                gate.set()
                self.assertTrue(completed.wait(1))

            with patch.object(refresh, "start_refresh_thread", side_effect=RuntimeError("thread quota exhausted")):
                result = app.test_client().post("/api/online-scripts/refresh", headers={"X-Token": TOKEN})
            self.assertEqual(result.status_code, 500)
            self.assertIn("thread quota exhausted", result.json["error"])
            self.assertEqual(app.test_client().get("/api/online-scripts/refresh-status", headers={"X-Token": TOKEN}).json["status"], "启动失败")

    def test_restart_running_refresh_becomes_unknown_and_page_shows_it(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import ONLINE_REFRESH_STATE_FILE, get_online_refresh_state

            ONLINE_REFRESH_STATE_FILE.write_text(json.dumps({
                "id": "stale", "running": True, "status": "运行中", "message": "正在同步",
                "error": "", "updated_at": "2026-11-14 00:00:00", "log_file": str(base_dir / "stale.log"),
            }), encoding="utf-8")
            state = get_online_refresh_state()
            self.assertFalse(state["running"])
            self.assertEqual(state["status"], "结果未知")
            self.assertIn("面板重启后", state["error"])
            page = app.test_client().get("/online-scripts", headers={"X-Token": TOKEN})
            self.assertEqual(page.status_code, 200)
            self.assertIn("结果未知", page.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
