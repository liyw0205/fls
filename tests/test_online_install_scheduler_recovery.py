import json
import re
import shutil
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

from tests.test_ui_route_components import ROOT, TOKEN, isolated_app


class OnlineInstallSchedulerRecoveryTests(unittest.TestCase):
    def partial_install(self, app, base_dir, command_failure=False):
        from fls_manager.models import load_tasks
        from fls_manager.online_scripts.constants import get_online_install_job
        from fls_manager.online_scripts.install import InstallCommandError, install_worker
        from fls_manager.online_scripts.source import save_online_script_cache

        item = {
            "id": "demo", "name": "Demo", "type": "raw", "link": "https://example.invalid/demo.sh",
            "link_name": "demo.sh", "install": "fixture install",
            "task_cron": [{"name": "Daily", "command": "task demo.sh", "cron": "0 0 * * *"}],
        }
        save_online_script_cache([item])
        with patch("fls_manager.online_scripts.install.download_online_script_logged", return_value=base_dir / "scripts" / "demo.sh") as download, patch(
            "fls_manager.online_scripts.install.run_logged_command",
            side_effect=InstallCommandError("install command failed", returncode=17) if command_failure else None,
        ) as command, patch(
            "fls_manager.online_scripts.tasks.reload_scheduler", side_effect=RuntimeError("scheduler unavailable"),
        ), patch("fls_manager.routes.online_scripts.install.start_install_thread", side_effect=install_worker):
            response = app.test_client().post(
                "/online-scripts/install/demo", data={"import_task": "1", "enable_task": "1", "task_indexes": "1"},
                headers={"X-Token": TOKEN},
            )
        self.assertEqual(response.status_code, 302)
        install_id = response.headers["Location"].split("?")[0].rsplit("/", 1)[1]
        info = get_online_install_job(install_id)
        self.assertFalse(info["running"])
        self.assertTrue(info["scheduler_reload_pending"])
        self.assertEqual(info["phase"], "failed" if command_failure else "scheduler_reload_pending")
        self.assertEqual(info["status"], "失败" if command_failure else "部分完成")
        self.assertEqual(info["returncode"], 17 if command_failure else 0)
        self.assertEqual([task["name"] for task in load_tasks()], ["Daily"])
        download.assert_called_once()
        command.assert_called_once()
        return install_id

    def test_partial_completion_survives_restart_and_recovers_without_reinstalling(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.models import load_tasks
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_RUNNING, get_online_install_job

            install_id = self.partial_install(app, base_dir)
            ONLINE_INSTALL_RUNNING.clear()
            client = app.test_client()
            headers = {"X-Token": TOKEN}
            payload = client.get(f"/api/online-scripts/log/{install_id}", headers=headers).json
            self.assertEqual(payload["status"], "部分完成")
            self.assertEqual(payload["phase"], "scheduler_reload_pending")
            self.assertTrue(payload["scheduler_reload_pending"])
            self.assertIn("scheduler unavailable", payload["error"])
            self.assertEqual(payload["recovery"]["label"], "重新加载调度器")
            action = f"/online-scripts/install-reload/{install_id}"
            self.assertEqual(payload["recovery"]["action"], action)
            for page in (f"/online-scripts/log/{install_id}", "/online-scripts"):
                html = client.get(page, headers=headers).get_data(as_text=True)
                self.assertIn(f'action="{action}"', html)
                self.assertIn('name="csrf_token"', html)
            repeated_install = client.post(f"/online-scripts/install-retry/{install_id}", headers=headers)
            self.assertEqual(repeated_install.status_code, 409)
            self.assertIn(action, repeated_install.get_data(as_text=True))
            with patch("fls_manager.routes.online_scripts.install.reload_scheduler") as reload_scheduler, patch(
                "fls_manager.online_scripts.install.download_online_script_logged",
            ) as download, patch("fls_manager.online_scripts.install.import_task_if_needed") as importer:
                recovered = client.post(action, headers=headers)
            self.assertEqual(recovered.status_code, 302)
            self.assertEqual(recovered.headers["Location"], f"/online-scripts/log/{install_id}")
            reload_scheduler.assert_called_once()
            download.assert_not_called()
            importer.assert_not_called()
            info = get_online_install_job(install_id)
            self.assertEqual(info["status"], "已完成")
            self.assertFalse(info["scheduler_reload_pending"])
            self.assertEqual(info["error"], "")
            self.assertEqual(info["scheduler_reload_error"], "")
            self.assertEqual(len(load_tasks()), 1)
            final_payload = client.get(f"/api/online-scripts/log/{install_id}", headers=headers).json
            self.assertNotIn("recovery", final_payload)
            with patch("fls_manager.routes.online_scripts.install.reload_scheduler") as reload_scheduler:
                repeated = client.post(action, headers=headers)
            self.assertEqual(repeated.status_code, 409)
            reload_scheduler.assert_not_called()

    def test_reload_and_persistence_failures_keep_recovery_available(self):
        for failure in ("reload", "persist"):
            with self.subTest(failure=failure), isolated_app() as (app, base_dir):
                from fls_manager.online_scripts.constants import get_online_install_job

                install_id = self.partial_install(app, base_dir)
                action = f"/online-scripts/install-reload/{install_id}"
                client = app.test_client()
                headers = {"X-Token": TOKEN, "X-Requested-With": "FLS-Ajax"}
                with patch(
                    "fls_manager.routes.online_scripts.install.reload_scheduler",
                    side_effect=RuntimeError("still unavailable") if failure == "reload" else None,
                ), patch(
                    "fls_manager.routes.online_scripts.install.persist_online_install",
                    side_effect=PermissionError("job read-only") if failure == "persist" else None,
                ):
                    failed = client.post(action, headers=headers)
                self.assertEqual(failed.status_code, 503 if failure == "reload" else 500)
                html = failed.get_data(as_text=True)
                self.assertIn("still unavailable" if failure == "reload" else "job read-only", html)
                self.assertIn(f'action="{action}"', html)
                self.assertIn('name="csrf_token"', html)
                self.assertTrue(get_online_install_job(install_id)["scheduler_reload_pending"])
                with patch("fls_manager.routes.online_scripts.install.reload_scheduler"):
                    recovered = client.post(action, headers=headers)
                self.assertEqual(recovered.status_code, 302)
                self.assertEqual(get_online_install_job(install_id)["status"], "已完成")

    def test_api_reload_reports_failure_then_recovers_without_reinstalling(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.models import load_tasks
            from fls_manager.online_scripts.constants import get_online_install_job

            install_id = self.partial_install(app, base_dir)
            client = app.test_client()
            with patch(
                "fls_manager.routes.online_scripts.install.reload_scheduler",
                side_effect=RuntimeError("scheduler unavailable"),
            ) as reload_scheduler, patch(
                "fls_manager.online_scripts.install.download_online_script_logged"
            ) as download, patch(
                "fls_manager.online_scripts.install.import_task_if_needed"
            ) as importer:
                failed = client.post(
                    f"/api/online-scripts/install-reload/{install_id}",
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(failed.status_code, 503)
                payload = failed.get_json()
                self.assertFalse(payload["ok"])
                self.assertTrue(payload["scheduler_reload_required"])
                self.assertEqual(payload["phase"], "scheduler_reload_pending")
                self.assertIn("scheduler unavailable", payload["msg"])
                self.assertEqual(payload["reload_url"], f"/api/online-scripts/install-reload/{install_id}")
                reload_scheduler.assert_called_once()
                download.assert_not_called()
                importer.assert_not_called()
                self.assertTrue(get_online_install_job(install_id)["scheduler_reload_pending"])

            with patch("fls_manager.routes.online_scripts.install.reload_scheduler") as reload_scheduler, patch(
                "fls_manager.online_scripts.install.download_online_script_logged"
            ) as download, patch("fls_manager.online_scripts.install.import_task_if_needed") as importer:
                recovered = client.post(
                    f"/api/online-scripts/install-reload/{install_id}",
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(recovered.status_code, 200)
                self.assertTrue(recovered.get_json()["ok"])
                reload_scheduler.assert_called_once()
                download.assert_not_called()
                importer.assert_not_called()
                repeated = client.post(
                    f"/api/online-scripts/install-reload/{install_id}",
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(repeated.status_code, 409)
                self.assertFalse(repeated.get_json()["ok"])
                reload_scheduler.assert_called_once()
            self.assertFalse(get_online_install_job(install_id)["scheduler_reload_pending"])
            self.assertEqual([task["name"] for task in load_tasks()], ["Daily"])

    def test_api_reload_requires_csrf_and_rejects_invalid_or_running_records(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_RUNNING

            install_id = self.partial_install(app, base_dir)
            client = app.test_client()
            with client.session_transaction() as session:
                session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                               security_verified=True, csrf_token="api-reload-csrf")
            with patch("fls_manager.routes.online_scripts.install.reload_scheduler") as reload_scheduler:
                invalid = client.post(
                    "/api/online-scripts/install-reload/not-an-id", headers={"X-Token": TOKEN},
                )
                self.assertEqual(invalid.status_code, 400)
                denied = client.post(f"/api/online-scripts/install-reload/{install_id}")
                self.assertEqual(denied.status_code, 400)
                ONLINE_INSTALL_RUNNING[install_id]["running"] = True
                running = client.post(
                    f"/api/online-scripts/install-reload/{install_id}",
                    headers={"X-CSRF-Token": "api-reload-csrf"},
                )
                self.assertEqual(running.status_code, 409)
                reload_scheduler.assert_not_called()

    def test_recovery_loads_saved_task_into_the_real_scheduler(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.models import load_tasks
            from fls_manager.online_scripts.constants import get_online_install_job
            from fls_manager.scheduler import get_scheduler_state
            from fls_manager.state import scheduler

            install_id = self.partial_install(app, base_dir)
            task_id = load_tasks()[0]["id"]
            self.assertIsNone(scheduler.get_job(f"task_{task_id}"))
            response = app.test_client().post(f"/online-scripts/install-reload/{install_id}", headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 302)
            self.assertIsNotNone(scheduler.get_job(f"task_{task_id}"))
            self.assertEqual(get_scheduler_state()["status"], "succeeded")
            self.assertEqual(get_scheduler_state()["scheduled_count"], 1)
            self.assertEqual(get_online_install_job(install_id)["status"], "已完成")

    def test_restart_before_install_completion_keeps_unknown_after_scheduler_recovery(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import (
                ONLINE_INSTALL_RUNNING, get_online_install_job, persist_online_install,
            )

            install_id = self.partial_install(app, base_dir)
            info = get_online_install_job(install_id)
            info.update(running=True, status="运行中", returncode=None, error="")
            persist_online_install(info)
            ONLINE_INSTALL_RUNNING.clear()
            self.assertEqual(get_online_install_job(install_id)["status"], "结果未知")
            with patch("fls_manager.routes.online_scripts.install.reload_scheduler"):
                response = app.test_client().post(f"/online-scripts/install-reload/{install_id}", headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 302)
            recovered = get_online_install_job(install_id)
            self.assertFalse(recovered["scheduler_reload_pending"])
            self.assertEqual(recovered["status"], "结果未知")
            self.assertIsNone(recovered["returncode"])
            payload = app.test_client().get(f"/api/online-scripts/log/{install_id}", headers={"X-Token": TOKEN}).json
            self.assertEqual(payload["recovery"]["label"], "重试安装")

    def test_reloading_tasks_preserves_an_independent_install_command_failure(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import get_online_install_job

            install_id = self.partial_install(app, base_dir, command_failure=True)
            client = app.test_client()
            headers = {"X-Token": TOKEN}
            before = client.get(f"/api/online-scripts/log/{install_id}", headers=headers).json
            self.assertIn("install command failed", before["error"])
            self.assertIn("scheduler unavailable", before["error"])
            with patch("fls_manager.routes.online_scripts.install.reload_scheduler"):
                response = client.post(f"/online-scripts/install-reload/{install_id}", headers=headers)
            self.assertEqual(response.status_code, 302)
            info = get_online_install_job(install_id)
            self.assertEqual(info["status"], "失败")
            self.assertEqual(info["returncode"], 17)
            self.assertIn("install command failed", info["error"])
            self.assertFalse(info["scheduler_reload_pending"])
            after = client.get(f"/api/online-scripts/log/{install_id}", headers=headers).json
            self.assertEqual(after["recovery"]["label"], "重试安装")
            self.assertNotIn("scheduler unavailable", after["error"])

    def test_concurrent_reloads_have_one_claim_and_release_it_after_completion(self):
        with isolated_app() as (app, base_dir):
            install_id = self.partial_install(app, base_dir)
            action = f"/online-scripts/install-reload/{install_id}"
            entered = threading.Event()
            release = threading.Event()
            responses = []

            def reload_scheduler():
                entered.set()
                self.assertTrue(release.wait(3))

            def post():
                responses.append(app.test_client().post(action, headers={"X-Token": TOKEN}))

            with patch("fls_manager.routes.online_scripts.install.reload_scheduler", side_effect=reload_scheduler) as reload:
                thread = threading.Thread(target=post)
                thread.start()
                try:
                    self.assertTrue(entered.wait(3))
                    duplicate = app.test_client().post(action, headers={"X-Token": TOKEN})
                    self.assertEqual(duplicate.status_code, 409)
                finally:
                    release.set()
                    thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
                reload.assert_called_once()
            self.assertEqual([response.status_code for response in responses], [302])

    def test_deleted_source_does_not_prevent_reloading_already_saved_tasks(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.source import save_online_script_cache

            install_id = self.partial_install(app, base_dir)
            save_online_script_cache([])
            with patch("fls_manager.routes.online_scripts.install.reload_scheduler"):
                response = app.test_client().post(f"/online-scripts/install-reload/{install_id}", headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 302)

    def test_missing_running_and_completed_records_do_not_reload(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_RUNNING

            install_id = self.partial_install(app, base_dir)
            client = app.test_client()
            with patch("fls_manager.routes.online_scripts.install.reload_scheduler") as reload_scheduler:
                missing = client.post("/online-scripts/install-reload/deleted", headers={"X-Token": TOKEN})
                self.assertEqual(missing.status_code, 404)
                for options in ({"running": True}, {"running": False, "scheduler_reload_pending": False, "status": "已完成"}):
                    ONLINE_INSTALL_RUNNING[install_id].update(options)
                    response = client.post(f"/online-scripts/install-reload/{install_id}", headers={"X-Token": TOKEN})
                    self.assertEqual(response.status_code, 409)
                reload_scheduler.assert_not_called()

    def test_recovery_requires_valid_csrf_session_and_permission(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import get_online_install_job

            install_id = self.partial_install(app, base_dir)
            action = f"/online-scripts/install-reload/{install_id}"
            client = app.test_client()
            headers = {"X-Requested-With": "FLS-Ajax"}
            with client.session_transaction() as session:
                session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                               security_verified=True, csrf_token="reload-csrf")
            with patch("fls_manager.routes.online_scripts.install.reload_scheduler") as reload_scheduler:
                denied = client.post(action, headers=headers)
                self.assertEqual(denied.status_code, 400)
                with client.session_transaction() as session:
                    session["token_expire_at"] = 1
                expired = client.post(action, data={"csrf_token": "reload-csrf"}, headers=headers)
                self.assertEqual(expired.status_code, 401)
                with client.session_transaction() as session:
                    session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                                   security_verified=True, csrf_token="reload-csrf")
                with patch("fls_manager.auth.query_token_enabled", return_value=False):
                    forbidden = client.post(action, query_string={"token": TOKEN}, data={"csrf_token": "reload-csrf"}, headers=headers)
                self.assertEqual(forbidden.status_code, 403)
                reload_scheduler.assert_not_called()
                self.assertTrue(get_online_install_job(install_id)["scheduler_reload_pending"])
                recovered = client.post(action, data={"csrf_token": "reload-csrf"}, headers=headers)
            self.assertEqual(recovered.status_code, 302)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise recovery rendering")
    def test_polling_renders_scheduler_recovery_action_and_label_with_csrf(self):
        with isolated_app() as (app, base_dir):
            install_id = self.partial_install(app, base_dir)
            client = app.test_client()
            headers = {"X-Token": TOKEN}
            html = client.get(f"/online-scripts/log/{install_id}", headers=headers).get_data(as_text=True)
            payload = client.get(f"/api/online-scripts/log/{install_id}", headers=headers).json
            script = next(script for script in re.findall(r"<script>(.*?)</script>", html, re.S) if "async function loadLog(" in script)
            source = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
            reader = source[source.index("async function flsReadJsonResponse("):source.index("async function flsReadTextResponse(")]
            result = subprocess.run(["node", "-e", r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const elements = Object.fromEntries(['installStatus', 'installError', 'installReturncode',
 'installStopAction', 'installRecoveryAction', 'installPollError', 'log', 'flsLogNewTip']
 .map(id => [id, {textContent: '', hidden: false, dataset: {}, style: {}}]));
const context = {
 window: {innerHeight: 800, scrollY: 0, addEventListener() {}, scrollTo() {}},
 document: {documentElement: {scrollHeight: 800}, getElementById: id => elements[id], querySelector: () => null},
 fetch: async () => ({ok: true, status: 200, json: async () => input.payload}),
 setInterval: () => 1, clearInterval() {}, flsGetCsrfToken: () => 'reload-csrf',
};
vm.createContext(context);
vm.runInContext(input.reader + input.script, context);
setImmediate(() => {
 assert.equal(elements.installStatus.textContent, '部分完成');
 assert.match(elements.installRecoveryAction.innerHTML, /重新加载调度器/);
 assert.ok(elements.installRecoveryAction.innerHTML.includes(input.payload.recovery.action));
 assert.match(elements.installRecoveryAction.innerHTML, /csrf_token.*reload-csrf/);
 assert.doesNotMatch(elements.installRecoveryAction.innerHTML, /重试安装/);
 assert.equal(context.window.__FLS_ACTIVE_LOG_INTERVAL__, null);
});
"""], input=json.dumps({"script": script, "reader": reader, "payload": payload}), capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise recovery AJAX")
    def test_polling_recovery_button_posts_json_reload_and_refreshes_status(self):
        with isolated_app() as (app, base_dir):
            install_id = self.partial_install(app, base_dir)
            client = app.test_client()
            headers = {"X-Token": TOKEN}
            html = client.get(f"/online-scripts/log/{install_id}", headers=headers).get_data(as_text=True)
            payload = client.get(f"/api/online-scripts/log/{install_id}", headers=headers).json
            script = next(script for script in re.findall(r"<script>(.*?)</script>", html, re.S) if "async function loadLog(" in script)
            source = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
            reader = source[source.index("async function flsReadJsonResponse("):source.index("async function flsReadTextResponse(")]
            result = subprocess.run(["node", "-e", r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const button = {disabled: false};
const form = {listeners: {}, querySelector: selector => selector === 'button' ? button : null,
  addEventListener(name, handler) { this.listeners[name] = handler; }};
const elements = Object.fromEntries(['installStatus', 'installError', 'installReturncode',
 'installStopAction', 'installPollError', 'log', 'flsLogNewTip']
 .map(id => [id, {textContent: '', hidden: false, dataset: {}, style: {}}]));
elements.installRecoveryAction = {textContent: '', hidden: false, dataset: {}, style: {}, innerHTML: '',
  querySelector: selector => selector === 'form[data-api-action]' ? form : null};
let calls = [];
let responses = [input.payload, {ok: true, request_id: input.payload.request_id, status: '已完成'},
  {running: false, status: '已完成', log: 'reloaded'}];
const context = {
 window: {innerHeight: 800, scrollY: 0, addEventListener() {}, scrollTo() {}},
 document: {documentElement: {scrollHeight: 800}, getElementById: id => elements[id], querySelector: () => null},
 fetch: async (url, options) => {
   calls.push({url, options});
   const value = responses.shift();
   return {ok: true, status: 200, json: async () => value};
 },
 setInterval: () => 1, clearInterval() {}, flsGetCsrfToken: () => 'reload-csrf',
};
vm.createContext(context);
vm.runInContext(input.reader + input.script, context);
(async () => {
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(form.listeners.submit, 'recovery form should use the JSON action');
  let prevented = false;
  await form.listeners.submit({preventDefault: () => { prevented = true; }});
  assert.equal(prevented, true);
  assert.equal(calls[1].url, input.payload.recovery.api_action);
  assert.equal(calls[1].options.method, 'POST');
  assert.equal(calls[1].options.headers['X-CSRF-Token'], 'reload-csrf');
  assert.equal(calls.length, 3);
  assert.equal(elements.log.textContent, 'reloaded');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""], input=json.dumps({"script": script, "reader": reader, "payload": payload}), capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
