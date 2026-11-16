import json
import re
import shutil
import subprocess
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.test_ui_route_components import ROOT, TOKEN, isolated_app


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

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise the polling script")
    def test_refresh_polling_surfaces_http_error_and_restores_retry_button(self):
        with isolated_app() as (app, _base_dir):
            response = app.test_client().get("/online-scripts", headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 200)
            scripts = re.findall(r"<script>(.*?)</script>", response.get_data(as_text=True), re.S)
            script = next(script for script in scripts if "async function updateOnlineRefreshStatus()" in script)
            source = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
            reader = source[source.index("async function flsReadJsonResponse("):source.index("async function flsReadTextResponse(")]
            result = subprocess.run(
                ["node", "-e", r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
(async () => {
for(const status of [401, 403, 409, 500, 200]) {
 const elements = {
  onlineRefreshStatusCard: {style: {}},
  onlineRefreshStatusText: {innerHTML: ''},
  onlineRefreshBtn: {disabled: true, textContent: '正在同步中...'},
};
const context = {
  window: {},
  document: {getElementById: id => elements[id]},
  fetch: async () => ({ok: status === 200, status, json: async () => {
    if(status === 200) throw new Error('not JSON');
    return {msg: 'backend diagnostic'};
  }}),
};
vm.createContext(context);
vm.runInContext(input.reader + input.script, context);
await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements.onlineRefreshStatusCard.style.display, 'block');
  assert.ok(elements.onlineRefreshStatusText.innerHTML.includes('HTTP ' + status));
  assert.match(elements.onlineRefreshStatusText.innerHTML, status === 200 ? /响应解析失败/ : /backend diagnostic/);
  if(status === 401 || status === 403) assert.match(elements.onlineRefreshStatusText.innerHTML, /重新登录/);
  else assert.match(elements.onlineRefreshStatusText.innerHTML, /重新同步/);
  assert.equal(elements.onlineRefreshBtn.disabled, false);
  assert.equal(elements.onlineRefreshBtn.textContent, '重新同步在线脚本源');
}
  console.log('refresh polling error recovery passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""],
                input=json.dumps({"script": script, "reader": reader}),
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
