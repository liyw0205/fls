import json
import re
import shutil
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

from tests.test_ui_route_components import ROOT, TOKEN, isolated_app


class OnlineInstallRecoveryTests(unittest.TestCase):
    def prepare(self, base_dir, **options):
        from fls_manager.online_scripts.constants import persist_online_install
        from fls_manager.online_scripts.source import save_online_script_cache
        from fls_manager.online_scripts.tasks import online_script_task_crons

        item = {
            "id": "demo", "name": "Demo", "type": "raw",
            "link": "https://example.invalid/demo.sh", "link_name": "demo.sh",
            "task_cron": [
                {"name": "Daily", "command": "task demo.sh"},
                {"name": "Hourly", "command": "task demo.sh hourly"},
            ],
        }
        save_online_script_cache([item])
        info = {
            "id": "previous", "script_id": "demo", "script_name": "Demo",
            "log_file": str(base_dir / "log" / "previous.log"),
            "running": False, "status": "结果未知", "start_time": 1,
            "returncode": None, "error": "panel restarted",
            "proxy_id": "", "import_task": True, "enable_task": False,
            "selected_task_indexes": ["2"], "force": True,
            "selected_tasks": [online_script_task_crons(item)[1]],
        }
        info.update(options)
        persist_online_install(info)
        return item, info

    def test_existing_target_requires_confirmation_then_completes_selected_import(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.models import load_tasks
            from fls_manager.online_scripts.constants import get_online_install_job
            from fls_manager.online_scripts.install import install_worker

            self.prepare(base_dir)
            target = base_dir / "scripts" / "demo.sh"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("original", encoding="utf-8")
            client = app.test_client()
            with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                confirmation = client.post(
                    "/online-scripts/install-retry/previous", headers={"X-Token": TOKEN},
                )
                self.assertEqual(confirmation.status_code, 200)
                self.assertIn("覆盖确认", confirmation.get_data(as_text=True))
                self.assertIn('action="/online-scripts/install-retry/previous"', confirmation.get_data(as_text=True))
                start.assert_not_called()
                self.assertFalse(get_online_install_job("previous").get("retried_by"))
                self.assertEqual(target.read_text(encoding="utf-8"), "original")

            def download(**kwargs):
                self.assertTrue(kwargs["force"])
                target.write_text("replacement", encoding="utf-8")
                return target

            with patch("fls_manager.online_scripts.install.download_online_script_logged", side_effect=download), patch(
                "fls_manager.online_scripts.tasks.reload_scheduler",
            ), patch(
                "fls_manager.routes.online_scripts.install.start_install_thread",
                side_effect=install_worker,
            ):
                response = client.post(
                    "/online-scripts/install-retry/previous", data={"force": "1"},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(response.status_code, 302)
            retry_id = response.headers["Location"].split("?")[0].rsplit("/", 1)[1]
            self.assertEqual(get_online_install_job(retry_id)["status"], "已完成")
            self.assertEqual(get_online_install_job(retry_id)["retry_of"], "previous")
            self.assertEqual(get_online_install_job(retry_id)["selected_tasks"][0]["name"], "Hourly")
            self.assertEqual(get_online_install_job("previous")["retried_by"], retry_id)
            self.assertEqual([task["name"] for task in load_tasks()], ["Hourly"])

    def test_consumed_retry_remains_conflict_after_child_completion_and_restart(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import (
                ONLINE_INSTALL_RUNNING, get_online_install_job, persist_online_install,
            )

            self.prepare(base_dir)
            client = app.test_client()
            with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                first = client.post("/online-scripts/install-retry/previous", headers={"X-Token": TOKEN})
                self.assertEqual(first.status_code, 302)
                child_id = start.call_args.kwargs["install_id"]
                child = get_online_install_job(child_id)
                child.update(running=False, status="已完成", returncode=0)
                ONLINE_INSTALL_RUNNING[child_id].update(child)
                persist_online_install(child)
                ONLINE_INSTALL_RUNNING.clear()
                repeated = client.post("/online-scripts/install-retry/previous", headers={"X-Token": TOKEN})
                self.assertEqual(repeated.status_code, 409)
                self.assertIn(f"/online-scripts/log/{child_id}", repeated.get_data(as_text=True))
                self.assertEqual(start.call_count, 1)
                payload = client.get("/api/online-scripts/log/previous", headers={"X-Token": TOKEN}).get_json()
                self.assertFalse(payload["recovery"]["available"])
                self.assertEqual(payload["recovery"]["href"], f"/online-scripts/log/{child_id}")
                page = client.get("/online-scripts/log/previous", headers={"X-Token": TOKEN}).get_data(as_text=True)
                self.assertNotIn('action="/online-scripts/install-retry/previous"', page)
                self.assertIn("查看重试记录", page)

    def test_retry_refuses_other_active_install_for_same_script(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_RUNNING

            self.prepare(base_dir)
            ONLINE_INSTALL_RUNNING["active"] = {"id": "active", "script_id": "demo", "running": True}
            with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                response = app.test_client().post(
                    "/online-scripts/install-retry/previous", headers={"X-Token": TOKEN},
                )
            self.assertEqual(response.status_code, 409)
            self.assertIn("/online-scripts/log/active", response.get_data(as_text=True))
            start.assert_not_called()

    def test_invalid_saved_task_indexes_do_not_fall_back_to_importing_all(self):
        for indexes in (["bad"], ["0"], ["3"], [1.5], [True], [], "2"):
            with self.subTest(indexes=indexes), isolated_app() as (app, base_dir):
                self.prepare(base_dir, selected_task_indexes=indexes)
                with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                    response = app.test_client().post(
                        "/online-scripts/install-retry/previous", headers={"X-Token": TOKEN},
                    )
                self.assertEqual(response.status_code, 409)
                self.assertIn("/online-scripts/install-select/demo", response.get_data(as_text=True))
                start.assert_not_called()
                payload = app.test_client().get(
                    "/api/online-scripts/log/previous", headers={"X-Token": TOKEN},
                ).get_json()
                self.assertFalse(payload["recovery"]["available"])
                self.assertEqual(payload["recovery"]["href"], "/online-scripts/install-select/demo")

    def test_changed_task_source_requires_new_selection(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.source import save_online_script_cache

            item, _info = self.prepare(base_dir)
            item["task_cron"].reverse()
            save_online_script_cache([item])
            with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                response = app.test_client().post(
                    "/online-scripts/install-retry/previous", headers={"X-Token": TOKEN},
                )
            self.assertEqual(response.status_code, 409)
            start.assert_not_called()
            page = app.test_client().get("/online-scripts", headers={"X-Token": TOKEN}).get_data(as_text=True)
            self.assertIn('href="/online-scripts/install-select/demo"', page)

    def test_unknown_status_remains_visible_when_recovery_snapshot_cannot_be_saved(self):
        with isolated_app() as (app, base_dir):
            self.prepare(base_dir, running=True, status="运行中")
            with patch("fls_manager.online_scripts.constants.persist_online_install", side_effect=OSError("disk full")):
                payload = app.test_client().get(
                    "/api/online-scripts/log/previous", headers={"X-Token": TOKEN},
                ).get_json()
            self.assertEqual(payload["status"], "结果未知")
            self.assertFalse(payload["running"])
            self.assertIn("disk full", payload["error"])

    def test_old_unreconciled_install_does_not_replace_newest_completed_attempt(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import latest_online_install_for_script, persist_online_install

            _item, info = self.prepare(base_dir, running=True, status="运行中")
            info.update(id="newest", running=False, status="已完成", returncode=0, start_time=2,
                        updated_at="2026-11-14 00:00:00")
            persist_online_install(info)
            latest_id, latest = latest_online_install_for_script("demo")
            self.assertEqual(latest_id, "newest")
            self.assertEqual(latest["status"], "已完成")

    def test_retry_persistence_failure_preserves_original_and_allows_resubmission(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import get_online_install_job

            self.prepare(base_dir)
            client = app.test_client()
            with patch("fls_manager.routes.online_scripts.install.persist_online_install", side_effect=OSError("disk full")), patch(
                "fls_manager.routes.online_scripts.install.start_install_thread",
            ) as start:
                response = client.post("/online-scripts/install-retry/previous", headers={"X-Token": TOKEN})
                self.assertEqual(response.status_code, 500)
                self.assertIn("disk full", response.get_data(as_text=True))
                start.assert_not_called()
            self.assertFalse(get_online_install_job("previous").get("retried_by"))
            with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                response = client.post("/online-scripts/install-retry/previous", headers={"X-Token": TOKEN})
                self.assertEqual(response.status_code, 302)
                start.assert_called_once()

    def test_retry_thread_start_failure_links_child_and_child_can_be_retried(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import get_online_install_job

            self.prepare(base_dir)
            client = app.test_client()
            with patch("fls_manager.routes.online_scripts.install.start_install_thread", side_effect=RuntimeError("thread quota")):
                response = client.post("/online-scripts/install-retry/previous", headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 500)
            child_id = get_online_install_job("previous")["retried_by"]
            self.assertEqual(get_online_install_job(child_id)["status"], "启动失败")
            with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                response = client.post(f"/online-scripts/install-retry/{child_id}", headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 302)
            start.assert_called_once()

    def test_retry_requires_csrf_and_live_session_before_state_mutation(self):
        with isolated_app() as (app, base_dir):
            self.prepare(base_dir)
            client = app.test_client()
            with client.session_transaction() as session:
                session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                               security_verified=True, csrf_token="retry-csrf")
            with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                denied = client.post("/online-scripts/install-retry/previous")
                self.assertEqual(denied.status_code, 400)
                start.assert_not_called()
                page = client.get("/online-scripts/log/previous").get_data(as_text=True)
                self.assertIn('name="csrf_token" value="retry-csrf"', page)
                with client.session_transaction() as session:
                    session["token_expire_at"] = 1
                expired = client.post(
                    "/online-scripts/install-retry/previous",
                    headers={"X-CSRF-Token": "retry-csrf", "X-Requested-With": "FLS-Ajax"},
                )
                self.assertEqual(expired.status_code, 401)
                start.assert_not_called()
                with client.session_transaction() as session:
                    session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                                   security_verified=True, csrf_token="retry-csrf")
                accepted = client.post(
                    "/online-scripts/install-retry/previous", data={"csrf_token": "retry-csrf"},
                    headers={"X-Requested-With": "FLS-Ajax"},
                )
                self.assertEqual(accepted.status_code, 302)
                start.assert_called_once()

    def test_concurrent_retry_claim_registers_only_one_child(self):
        with isolated_app() as (app, base_dir):
            self.prepare(base_dir)
            entered = threading.Event()
            release = threading.Event()
            responses = []

            def blocked(**_kwargs):
                entered.set()
                self.assertTrue(release.wait(3))

            def request_retry():
                responses.append(app.test_client().post(
                    "/online-scripts/install-retry/previous", headers={"X-Token": TOKEN},
                ))

            with patch("fls_manager.routes.online_scripts.install.start_install_thread", side_effect=blocked) as start:
                thread = threading.Thread(target=request_retry)
                thread.start()
                try:
                    self.assertTrue(entered.wait(3))
                    duplicate = app.test_client().post(
                        "/online-scripts/install-retry/previous", headers={"X-Token": TOKEN},
                    )
                    self.assertEqual(duplicate.status_code, 409)
                finally:
                    release.set()
                    thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
                start.assert_called_once()
            self.assertEqual([response.status_code for response in responses], [302])

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise log polling")
    def test_polling_preserves_logs_and_stops_on_permission_or_missing_errors(self):
        with isolated_app() as (app, base_dir):
            self.prepare(base_dir)
            unauthorized = app.test_client().get("/api/online-scripts/log/previous")
            self.assertEqual(unauthorized.status_code, 401)
            self.assertEqual(unauthorized.get_json()["msg"], "登录已过期")
            html = app.test_client().get("/online-scripts/log/previous", headers={"X-Token": TOKEN}).get_data(as_text=True)
            self.assertIn('id="installPollLogin" class="btn btn-primary" hidden>重新登录</a>', html)
            self.assertIn('id="installPollLogs" class="btn btn-orange" href="/logs" hidden>查看日志管理</a>', html)
            script = next(script for script in re.findall(r"<script>(.*?)</script>", html, re.S)
                          if "async function loadLog()" in script)
            source = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
            reader = source[source.index("async function flsReadJsonResponse("):source.index("async function flsReadTextResponse(")]
            result = subprocess.run(["node", "-e", r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
(async () => {
  for(const status of [401, 403, 404, 409, 500, 200]) {
    const elements = Object.fromEntries(['installStatus', 'installError', 'installReturncode',
      'installStopAction', 'installRecoveryAction', 'installPollError', 'installPollLogin', 'installPollLogs', 'installPollRetry', 'log', 'flsLogNewTip']
      .map(id => [id, {textContent: '', hidden: false, dataset: {}, style: {}}]));
    elements.log.textContent = 'preserved output';
    const context = {
      window: {innerHeight: 800, scrollY: 0, addEventListener() {}, scrollTo() {}},
      document: {documentElement: {scrollHeight: 800}, getElementById: id => elements[id]},
      location: {pathname: '/online-scripts/log/previous', search: '?back=%2Fpull'},
      fetch: async () => ({ok: status === 200, status,
        json: async () => {
          if(status === 200) throw new Error('not JSON');
          return {msg: 'backend diagnostic'};
        }}),
      setInterval: () => 1, clearInterval() {},
    };
    vm.createContext(context);
    vm.runInContext(input.reader + input.script, context);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(elements.log.textContent, 'preserved output');
    assert.equal(elements.installPollError.hidden, false);
    const permanent = [401, 403, 404].includes(status);
    assert.equal(context.window.__FLS_ACTIVE_LOG_INTERVAL__, null);
    const denied = [401, 403].includes(status);
    assert.equal(elements.installPollLogin.hidden, !denied);
    assert.equal(elements.installPollLogs.hidden, status !== 404);
    assert.equal(elements.installPollRetry.hidden, status === 404);
    assert.equal(elements.installPollRetry.disabled, false);
    if(denied) assert.equal(elements.installPollLogin.href,
      '/login?next=%2Fonline-scripts%2Flog%2Fprevious%3Fback%3D%252Fpull');
    if(status === 404) assert.equal(elements.installPollLogs.href, '/logs');
    if(permanent) assert.equal(elements.installRecoveryAction.hidden, true);
    assert.match(elements.installPollError.textContent, status === 200 ? /响应解析失败/ : /backend diagnostic/);
    if(status !== 404) {
      context.fetch = async () => ({ok: true, status: 200, json: async () => ({
        running: true, status: '安装中', log: 'running output',
      })});
      await elements.installPollRetry.onclick();
      assert.equal(elements.installStopAction.hidden, false);
      assert.equal(elements.installRecoveryAction.hidden, true);
      assert.equal(elements.installPollError.hidden, true);
      assert.equal(elements.installPollLogin.hidden, true);
      assert.equal(context.window.__FLS_ACTIVE_LOG_INTERVAL__, 1);
      context.fetch = async () => ({ok: true, status: 200, json: async () => ({
        running: false, status: '结果未知', log: 'recovered output',
        recovery: {available: false, reason: 'already retried', href: '/online-scripts/log/child', label: 'child'},
      })});
      await elements.installPollRetry.onclick();
      assert.equal(elements.installPollError.hidden, true);
      assert.equal(elements.installPollLogin.hidden, true);
      assert.equal(elements.log.textContent, 'recovered output');
      assert.match(elements.installRecoveryAction.innerHTML, /online-scripts\/log\/child/);
      assert.equal(elements.installRecoveryAction.hidden, false);
      assert.equal(context.window.__FLS_ACTIVE_LOG_INTERVAL__, null);
    }
  }
  {
    const elements = Object.fromEntries(['installStatus', 'installError', 'installReturncode',
      'installStopAction', 'installRecoveryAction', 'installPollError', 'installPollLogin', 'installPollLogs', 'installPollRetry', 'log']
      .map(id => [id, {textContent: 'preserved', hidden: false, dataset: {}, style: {}}]));
    let resolve;
    let requests = 0;
    let intervals = 0;
    const context = {
      window: {innerHeight: 800, scrollY: 0, addEventListener() {}, scrollTo() {}},
      document: {documentElement: {scrollHeight: 800}, getElementById: id => elements[id]},
      fetch: () => { requests++; return new Promise(done => { resolve = done; }); },
      setInterval: () => { intervals++; return 1; }, clearInterval() {},
    };
    vm.createContext(context);
    vm.runInContext(input.reader + input.script, context);
    await elements.installPollRetry.onclick();
    assert.equal(requests, 1, 'manual recovery must not overlap an active request');
    elements.installPollError = {};
    resolve({ok: true, status: 200, json: async () => ({running: true, log: 'old page output'})});
    await new Promise(done => setImmediate(done));
    assert.equal(elements.log.textContent, 'preserved');
    assert.equal(intervals, 0, 'detached page must not start polling again');
  }
  console.log('log polling error paths passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""], input=json.dumps({"script": script, "reader": reader}), capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
