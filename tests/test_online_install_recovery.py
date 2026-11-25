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
                self.assertIn('action="/online-scripts/install-retry/previous?back=/online-scripts"', confirmation.get_data(as_text=True))
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
            from fls_manager.online_scripts.constants import _load_persisted_online_install

            self.prepare(base_dir, running=True, status="运行中")
            with patch("fls_manager.online_scripts.constants.persist_online_install", side_effect=OSError("disk full")):
                payload = app.test_client().get(
                    "/api/online-scripts/log/previous", headers={"X-Token": TOKEN},
                ).get_json()
            self.assertEqual(payload["status"], "结果未知")
            self.assertFalse(payload["running"])
            self.assertIn("disk full", payload["error"])
            self.assertIn("回读状态与预期不一致", payload["error"])
            self.assertTrue(payload["persistence_error"])
            self.assertFalse(payload["recovery"]["available"])
            persisted = _load_persisted_online_install("previous")
            self.assertEqual(persisted["status"], "运行中")

            recovered = app.test_client().get(
                "/api/online-scripts/log/previous", headers={"X-Token": TOKEN},
            ).get_json()
            self.assertNotIn("persistence_error", recovered)
            self.assertTrue(recovered["recovery"]["available"])
            self.assertEqual(_load_persisted_online_install("previous")["status"], "结果未知")

    def test_unknown_status_write_then_error_is_confirmed_by_persisted_snapshot(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import (
                _load_persisted_online_install, persist_online_install,
            )

            self.prepare(base_dir, running=True, status="运行中")

            def persist_then_raise(info):
                persist_online_install(info)
                raise OSError("write acknowledgement lost")

            client = app.test_client()
            with patch(
                "fls_manager.online_scripts.constants.persist_online_install",
                side_effect=persist_then_raise,
            ) as persist:
                payload = client.get(
                    "/api/online-scripts/log/previous", headers={"X-Token": TOKEN},
                ).get_json()
                self.assertEqual(payload["status"], "结果未知")
                self.assertFalse(payload["running"])
                self.assertIn("面板重启后无法确认", payload["error"])
                self.assertNotIn("保存恢复状态失败", payload["error"])
                persist.assert_called_once()

            stored = _load_persisted_online_install("previous")
            self.assertEqual(stored["status"], "结果未知")
            self.assertFalse(stored["running"])
            self.assertIn("面板重启后无法确认", stored["error"])
            self.assertNotIn("保存恢复状态失败", stored["error"])
            self.assertIn("安装日志尚未生成", payload["error"])

            page = client.get("/online-scripts/log/previous", headers={"X-Token": TOKEN})
            self.assertEqual(page.status_code, 200)
            self.assertIn("确认状态后再决定是否重试", page.get_data(as_text=True))
            self.assertIn('action="/online-scripts/install-retry/previous"', page.get_data(as_text=True))

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise install receipt recovery UI")
    def test_unknown_receipt_ui_keeps_retry_disabled_until_state_query_recovers(self):
        with isolated_app() as (app, base_dir):
            self.prepare(base_dir, running=True, status="运行中")
            log_file = base_dir / "log" / "previous.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text("installer started before panel restart\n", encoding="utf-8")
            client = app.test_client()

            with patch(
                "fls_manager.online_scripts.constants.persist_online_install",
                side_effect=OSError("disk full"),
            ):
                page = client.get("/online-scripts/log/previous", headers={"X-Token": TOKEN})
                self.assertEqual(page.status_code, 200)
                initial = client.get(
                    "/api/online-scripts/log/previous", headers={"X-Token": TOKEN},
                ).get_json()

            self.assertEqual(initial["install_id"], "previous")
            self.assertEqual(initial["status"], "结果未知")
            self.assertTrue(initial["persistence_error"])
            self.assertFalse(initial["recovery"]["available"])

            recovered = client.get(
                "/api/online-scripts/log/previous?state_only=1", headers={"X-Token": TOKEN},
            ).get_json()
            self.assertNotIn("persistence_error", recovered)
            self.assertTrue(recovered["recovery"]["available"])

            html = page.get_data(as_text=True)
            script = next(
                value for value in re.findall(r"<script>(.*?)</script>", html, re.S)
                if "async function loadLog(" in value
            )
            source = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
            reader = source[
                source.index("function flsResponseErrorDiagnostic("):
                source.index("async function flsReadTextResponse(")
            ]
            result = subprocess.run(
                ["node", "-e", r"""
const vm=require('node:vm'),assert=require('node:assert/strict');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const names=['installStatus','installPhase','installError','installReturncode','installStopAction',
  'installRecoveryAction','installPollError','installPollLogin','installPollLogs','installPollRetry',
  'installStateQuery','installStopQuery','log','flsLogNewTip'];
const elements=Object.fromEntries(names.map(id=>[id,{hidden:id==='installPollError'||id==='installStateQuery',
  disabled:false,textContent:'',style:{},dataset:{},innerHTML:'',handlers:{},
  addEventListener(name,handler){this.handlers[name]=handler;},querySelector(){return null;}}]));
const calls=[];let responseIndex=0;
function response(payload){return {ok:true,status:200,text:async()=>JSON.stringify(payload)};}
const responses=[input.initial,{http_status:503,msg:'temporary install state outage'},input.recovered];
const context={
  window:{innerHeight:800,scrollY:0,addEventListener(){},scrollTo(){}},
  document:{documentElement:{scrollHeight:800},getElementById:id=>elements[id],querySelector:()=>null},
  location:{pathname:'/online-scripts/log/previous',search:''},
  URLSearchParams,
  flsGetCsrfToken:()=> 'csrf-token',
  fetch:async url=>{calls.push(url);const value=responses[responseIndex++];
    return value.http_status?{ok:false,status:value.http_status,text:async()=>JSON.stringify({msg:value.msg})}:response(value);},
  setInterval:()=>1,clearInterval(){},setTimeout:()=>1,clearTimeout(){},
};
vm.createContext(context);vm.runInContext(input.reader+input.script,context);
const flush=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  await flush();
  assert.equal(calls[0],'/api/online-scripts/log/previous?lines=1600');
  assert.equal(elements.installStatus.textContent,'结果未知');
  assert.match(elements.installPollError.textContent,/已禁用重试/);
  assert.equal(elements.installPollError.hidden,false);
  assert.equal(elements.installStateQuery.hidden,false);
  assert.doesNotMatch(elements.installRecoveryAction.innerHTML,/install-retry/);
  assert.equal(elements.log.textContent,input.initial.log);

  await elements.installStateQuery.handlers.click();
  assert.equal(calls[1],'/api/online-scripts/log/previous?state_only=1');
  assert.match(elements.installPollError.textContent,/temporary install state outage/);
  assert.equal(elements.installStateQuery.hidden,false);
  assert.doesNotMatch(elements.installRecoveryAction.innerHTML,/install-retry/);
  assert.equal(elements.log.textContent,input.initial.log);

  await elements.installStateQuery.handlers.click();
  assert.equal(calls[2],'/api/online-scripts/log/previous?state_only=1');
  assert.equal(elements.installStateQuery.hidden,true);
  assert.match(elements.installRecoveryAction.innerHTML,/install-retry\/previous/);
  assert.match(elements.installRecoveryAction.innerHTML,/csrf_token/);
  assert.equal(elements.installRecoveryAction.dataset.ready,'1');
  assert.equal(elements.log.textContent,input.initial.log);
  console.log('unknown install receipt UI recovery passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""],
                input=json.dumps({"script": script, "reader": reader, "initial": initial, "recovered": recovered}),
                capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

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

    def test_stable_install_request_replay_returns_original_log_without_starting_twice(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import get_online_install_job
            from fls_manager.online_scripts.source import save_online_script_cache

            item, _ = self.prepare(base_dir)
            save_online_script_cache([item])
            request_id = "a" * 32
            client = app.test_client()
            with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                first = client.post(
                    "/online-scripts/install/demo",
                    data={"request_id": request_id, "proxy_id": "", "import_task": "1", "task_indexes": "1"},
                    headers={"X-Token": TOKEN},
                )
                repeated = client.post(
                    "/online-scripts/install/demo",
                    data={"request_id": request_id, "proxy_id": "", "import_task": "1", "task_indexes": "1"},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(first.status_code, 302)
            self.assertEqual(repeated.status_code, 302)
            self.assertEqual(start.call_count, 1)
            self.assertEqual(first.headers["Location"], repeated.headers["Location"])
            self.assertEqual(get_online_install_job(request_id)["id"], request_id)

    def test_stable_install_request_option_conflict_has_zero_start_side_effect(self):
        with isolated_app() as (app, base_dir):
            item, _ = self.prepare(base_dir)
            from fls_manager.online_scripts.source import save_online_script_cache
            save_online_script_cache([item])
            request_id = "b" * 32
            client = app.test_client()
            with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                first = client.post(
                    "/online-scripts/install/demo",
                    data={"request_id": request_id, "proxy_id": "", "import_task": "1", "task_indexes": "1"},
                    headers={"X-Token": TOKEN},
                )
                conflict = client.post(
                    "/online-scripts/install/demo",
                    data={"request_id": request_id, "proxy_id": "", "import_task": "1", "task_indexes": "2"},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(first.status_code, 302)
            self.assertEqual(conflict.status_code, 409)
            self.assertEqual(start.call_count, 1)

    def test_stable_install_request_thread_start_failure_is_queryable_without_retrying(self):
        with isolated_app() as (app, base_dir):
            item, _ = self.prepare(base_dir)
            from fls_manager.online_scripts.source import save_online_script_cache
            from fls_manager.online_scripts.constants import get_online_install_job
            save_online_script_cache([item])
            request_id = "c" * 32
            client = app.test_client()
            with patch("fls_manager.routes.online_scripts.install.start_install_thread", side_effect=RuntimeError("thread unavailable")) as start:
                first = client.post(
                    "/online-scripts/install/demo",
                    data={"request_id": request_id, "proxy_id": ""},
                    headers={"X-Token": TOKEN},
                )
                repeated = client.post(
                    "/online-scripts/install/demo",
                    data={"request_id": request_id, "proxy_id": ""},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(first.status_code, 500)
            self.assertEqual(repeated.status_code, 302)
            self.assertEqual(start.call_count, 1)
            self.assertEqual(get_online_install_job(request_id)["status"], "启动失败")

    def test_api_install_retries_same_request_after_unpublished_start_receipt_failure(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import constants
            from fls_manager.online_scripts.source import save_online_script_cache

            item, _ = self.prepare(base_dir)
            save_online_script_cache([item])
            request_id = "7" * 32
            client = app.test_client()
            with patch(
                "fls_manager.routes.online_scripts.install.persist_online_install",
                side_effect=OSError("install receipt disk full"),
            ), patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                failed = client.post(
                    "/api/online-scripts/install/demo",
                    json={"request_id": request_id, "force": True}, headers={"X-Token": TOKEN},
                )
            self.assertEqual(failed.status_code, 500)
            self.assertEqual(failed.json["status"], "启动失败")
            self.assertEqual(failed.json["phase"], "startup_receipt_failed")
            self.assertFalse(failed.json["running"])
            self.assertFalse(failed.json["accepted"])
            self.assertTrue(failed.json["retry_allowed"])
            self.assertNotIn("log_url", failed.json)
            self.assertIsNone(constants._load_persisted_online_install(request_id))
            start.assert_not_called()

            constants.ONLINE_INSTALL_RUNNING.clear()
            with patch(
                "fls_manager.routes.online_scripts.install.persist_online_install",
                wraps=constants.persist_online_install,
            ), patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                retried = client.post(
                    "/api/online-scripts/install/demo",
                    json={"request_id": request_id, "force": True}, headers={"X-Token": TOKEN},
                )
            self.assertEqual(retried.status_code, 200)
            self.assertFalse(retried.json.get("replayed", False))
            self.assertEqual(retried.json["request_id"], request_id)
            self.assertTrue(retried.json["running"])
            self.assertEqual(start.call_count, 1)
            self.assertTrue(constants._load_persisted_online_install(request_id)["running"])

    def test_api_install_status_read_failure_after_start_is_recoverable_without_duplicate_start(self):
        with isolated_app() as (app, base_dir):
            item, _ = self.prepare(base_dir)
            from fls_manager.routes.online_scripts import install as install_route
            from fls_manager.online_scripts.source import save_online_script_cache

            save_online_script_cache([item])
            request_id = "8" * 32
            client = app.test_client()
            real_get = install_route.get_online_install_job
            started = False
            read_failed = False

            def start_thread(**_kwargs):
                nonlocal started
                started = True

            def fail_first_read_after_start(install_id):
                nonlocal read_failed
                if started and not read_failed:
                    read_failed = True
                    raise OSError("temporary install state read failure")
                return real_get(install_id)

            with patch.object(install_route, "get_online_install_job", side_effect=fail_first_read_after_start), patch.object(
                install_route, "start_install_thread", side_effect=start_thread,
            ) as start:
                first = client.post(
                    "/api/online-scripts/install/demo",
                    json={"request_id": request_id, "force": True}, headers={"X-Token": TOKEN},
                )
                self.assertEqual(first.status_code, 503)
                self.assertFalse(first.json["ok"])
                self.assertTrue(first.json["accepted"])
                self.assertFalse(first.json["retry_allowed"])
                self.assertEqual(first.json["request_id"], request_id)
                self.assertEqual(first.json["install_id"], request_id)
                self.assertEqual(first.json["log_url"], f"/api/online-scripts/log/{request_id}")
                self.assertEqual(
                    first.json["state_url"],
                    f"/api/online-scripts/log/{request_id}?state_only=1",
                )
                self.assertIn("安装已受理", first.json["msg"])

                replay = client.post(
                    "/api/online-scripts/install/demo",
                    json={"request_id": request_id, "force": True}, headers={"X-Token": TOKEN},
                )
                self.assertEqual(replay.status_code, 200)
                self.assertTrue(replay.json["replayed"])
                self.assertEqual(replay.json["request_id"], request_id)

                state = client.get(
                    first.json["state_url"], headers={"X-Token": TOKEN},
                )
                self.assertEqual(state.status_code, 200)
                self.assertTrue(state.json["running"])
                self.assertEqual(state.json["install_id"], request_id)
                start.assert_called_once()

    def test_api_startup_failure_receipt_write_then_error_is_recovered_from_disk(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import constants
            from fls_manager.online_scripts.source import save_online_script_cache

            item, _ = self.prepare(base_dir)
            save_online_script_cache([item])
            request_id = "4" * 32
            persist = constants.persist_online_install
            prepared = True
            failure_receipt = True

            def fail_then_publish_before_error(info):
                nonlocal prepared, failure_receipt
                if info.get("id") == request_id and info.get("phase") == "prepared" and prepared:
                    prepared = False
                    raise OSError("prepared receipt rejected")
                if (
                    info.get("id") == request_id
                    and info.get("phase") == "startup_receipt_failed"
                    and failure_receipt
                ):
                    failure_receipt = False
                    persist(info)
                    raise OSError("failure receipt acknowledgement lost")
                return persist(info)

            client = app.test_client()
            with patch(
                "fls_manager.routes.online_scripts.install.persist_online_install",
                side_effect=fail_then_publish_before_error,
            ), patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                failed = client.post(
                    "/api/online-scripts/install/demo",
                    json={"request_id": request_id, "force": True}, headers={"X-Token": TOKEN},
                )

            self.assertEqual(failed.status_code, 500)
            self.assertNotIn("accepted", failed.json)
            self.assertEqual(failed.json["log_url"], f"/api/online-scripts/log/{request_id}")
            persisted = constants._load_persisted_online_install(request_id)
            self.assertEqual(persisted["phase"], "startup_receipt_failed")
            self.assertNotIn("启动失败回执也未能保存", persisted["error"])
            start.assert_not_called()

            constants.ONLINE_INSTALL_RUNNING.clear()
            state = client.get(
                f"/api/online-scripts/log/{request_id}?state_only=1", headers={"X-Token": TOKEN},
            )
            self.assertEqual(state.status_code, 200)
            self.assertTrue(state.json["recovery"]["available"])
            with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                retried = client.post(
                    f"/online-scripts/install-start-retry/{request_id}", headers={"X-Token": TOKEN},
                )
            self.assertEqual(retried.status_code, 302)
            start.assert_called_once()

    def test_startup_receipt_retry_rejects_changed_script_contents(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import constants
            from fls_manager.online_scripts.source import save_online_script_cache

            item, _ = self.prepare(base_dir)
            item["install"] = "original install command"
            save_online_script_cache([item])
            request_id = "6" * 32
            persist = constants.persist_online_install
            fail_prepared = True

            def reject_first_prepared_receipt(info):
                nonlocal fail_prepared
                if info.get("id") == request_id and info.get("phase") == "prepared" and fail_prepared:
                    fail_prepared = False
                    raise OSError("install receipt disk full")
                return persist(info)

            client = app.test_client()
            with patch(
                "fls_manager.routes.online_scripts.install.persist_online_install",
                side_effect=reject_first_prepared_receipt,
            ), patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                failed = client.post(
                    "/api/online-scripts/install/demo",
                    json={"request_id": request_id, "force": True}, headers={"X-Token": TOKEN},
                )
                self.assertEqual(failed.status_code, 500)
                self.assertEqual(failed.json["phase"], "startup_receipt_failed")
                self.assertFalse(failed.json["running"])
                failed_receipt = constants._load_persisted_online_install(request_id)
                self.assertEqual(failed_receipt["status"], "启动失败")
                original_fingerprint = failed_receipt["script_fingerprint"]

                item["install"] = "changed install command"
                save_online_script_cache([item])
                retry = client.post(
                    f"/online-scripts/install-start-retry/{request_id}", headers={"X-Token": TOKEN},
                )
                self.assertEqual(retry.status_code, 409)
                self.assertIn("脚本源内容已变化", retry.get_data(as_text=True))
                start.assert_not_called()
                persisted = constants._load_persisted_online_install(request_id)
                self.assertEqual(persisted["phase"], "startup_receipt_failed")
                self.assertEqual(persisted["script_fingerprint"], original_fingerprint)

    def test_startup_receipt_retry_requires_auth_and_csrf(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import constants

            self.prepare(base_dir)
            request_id = "5" * 32
            persist = constants.persist_online_install
            failed_prepared = False

            def fail_prepared_once(info):
                nonlocal failed_prepared
                if info.get("id") == request_id and info.get("phase") == "prepared" and not failed_prepared:
                    failed_prepared = True
                    raise OSError("install receipt disk full")
                return persist(info)

            client = app.test_client()
            with patch(
                "fls_manager.routes.online_scripts.install.persist_online_install",
                side_effect=fail_prepared_once,
            ), patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                failed = client.post(
                    "/api/online-scripts/install/demo",
                    json={"request_id": request_id, "force": True}, headers={"X-Token": TOKEN},
                )
                self.assertEqual(failed.status_code, 500)
                action = f"/online-scripts/install-start-retry/{request_id}"
                csrf = "startup-retry-csrf"
                with client.session_transaction() as session:
                    session["csrf_token"] = csrf
                self.assertEqual(client.post(action, data={"csrf_token": "wrong"}).status_code, 400)
                unauthenticated = client.post(action, data={"csrf_token": csrf})
                self.assertEqual(unauthenticated.status_code, 302)
                self.assertIn("/login?next=", unauthenticated.location)

                with client.session_transaction() as session:
                    session.update(
                        token=TOKEN,
                        token_expire_at=int(time.time()) + 3600,
                        security_verified=True,
                        csrf_token=csrf,
                    )
                self.assertEqual(client.post(action, data={"csrf_token": "wrong"}).status_code, 400)
                self.assertEqual(constants._load_persisted_online_install(request_id)["phase"], "startup_receipt_failed")
                start.assert_not_called()

                restored = client.post(action, data={"csrf_token": csrf})
                self.assertEqual(restored.status_code, 302)
                self.assertEqual(restored.location.split("?", 1)[0], f"/online-scripts/log/{request_id}")
                self.assertEqual(start.call_count, 1)
                repeated = client.post(action, data={"csrf_token": csrf})
                self.assertEqual(repeated.status_code, 409)
                self.assertEqual(start.call_count, 1)

    def test_startup_receipt_failure_is_protected_from_history_pruning(self):
        with isolated_app() as (_app, _base_dir):
            from fls_manager.online_scripts.constants import (
                ONLINE_INSTALL_JOBS_FILE,
                ONLINE_INSTALL_LIMIT,
                persist_online_install,
            )

            for index in range(ONLINE_INSTALL_LIMIT):
                install_id = f"{index + 1:032x}"
                persist_online_install({
                    "id": install_id,
                    "script_id": "demo",
                    "running": False,
                    "status": "已完成",
                    "phase": "completed",
                    "updated_at": f"2026-11-23 00:{index:02d}:00",
                })

            retryable_id = "f" * 32
            persist_online_install({
                "id": retryable_id,
                "script_id": "demo",
                "running": False,
                "status": "启动失败",
                "phase": "startup_receipt_failed",
                "script_fingerprint": "a" * 64,
                "updated_at": "2026-11-22 00:00:00",
            })

            stored = json.loads(ONLINE_INSTALL_JOBS_FILE.read_text(encoding="utf-8"))
            self.assertEqual(len(stored), ONLINE_INSTALL_LIMIT)
            self.assertIn(retryable_id, stored)

    def test_unknown_install_receipt_remains_queryable_after_history_pruning(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_JOBS_FILE, persist_online_install
            from fls_manager.online_scripts.source import save_online_script_cache

            save_online_script_cache([{
                "id": "demo", "name": "Demo", "type": "raw",
                "link": "https://example.invalid/demo.sh", "link_name": "demo.sh",
            }])
            unknown_id = "a" * 32
            client = app.test_client()
            with patch("fls_manager.online_scripts.constants.ONLINE_INSTALL_LIMIT", 3):
                persist_online_install({
                    "id": unknown_id, "script_id": "demo", "running": False,
                    "status": "结果未知", "phase": "unknown",
                    "updated_at": "2026-11-22 00:00:00",
                })
                for index in range(3):
                    install_id = f"{index + 1:032x}"
                    persist_online_install({
                        "id": install_id, "script_id": "demo", "running": False,
                        "status": "已完成", "phase": "completed",
                        "updated_at": f"2026-11-23 00:0{index}:00",
                    })

            stored = json.loads(ONLINE_INSTALL_JOBS_FILE.read_text(encoding="utf-8"))
            self.assertIn(unknown_id, stored)
            state = client.get(
                f"/api/online-scripts/log/{unknown_id}?state_only=1", headers={"X-Token": TOKEN},
            )
            self.assertEqual(state.status_code, 200)
            self.assertEqual(state.json["status"], "结果未知")
            self.assertTrue(state.json["recovery"]["available"])
            page = client.get(f"/online-scripts/log/{unknown_id}", headers={"X-Token": TOKEN})
            self.assertEqual(page.status_code, 200)
            self.assertIn(f"/online-scripts/install-retry/{unknown_id}", page.get_data(as_text=True))
            with client.session_transaction() as session:
                session.update(
                    token=TOKEN, token_expire_at=int(time.time()) + 3600,
                    security_verified=True, csrf_token="unknown-retry-csrf",
                )
            with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                rejected = client.post(
                    f"/online-scripts/install-retry/{unknown_id}", data={"csrf_token": "wrong"},
                )
                self.assertEqual(rejected.status_code, 400)
                start.assert_not_called()
                recovered = client.post(
                    f"/online-scripts/install-retry/{unknown_id}",
                    data={"csrf_token": "unknown-retry-csrf"},
                )
                self.assertEqual(recovered.status_code, 302)
                start.assert_called_once()
                retry_id = start.call_args.kwargs["install_id"]
                self.assertIn(f"/online-scripts/log/{retry_id}", recovered.location)
                stored = json.loads(ONLINE_INSTALL_JOBS_FILE.read_text(encoding="utf-8"))
                self.assertEqual(stored[unknown_id]["retried_by"], retry_id)
                self.assertEqual(stored[retry_id]["retry_of"], unknown_id)
                replay = client.post(
                    f"/online-scripts/install-retry/{unknown_id}",
                    data={"csrf_token": "unknown-retry-csrf"},
                )
                self.assertEqual(replay.status_code, 409)
                self.assertEqual(start.call_count, 1)

    def test_unknown_install_claim_fails_closed_when_all_history_slots_are_unresolved(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_JOBS_FILE, persist_online_install
            from fls_manager.online_scripts.source import save_online_script_cache

            save_online_script_cache([{
                "id": "demo", "name": "Demo", "type": "raw",
                "link": "https://example.invalid/demo.sh", "link_name": "demo.sh",
            }])
            client = app.test_client()
            unknown_ids = [f"{index + 1:032x}" for index in range(2)]
            with patch("fls_manager.online_scripts.constants.ONLINE_INSTALL_LIMIT", 2):
                for index, install_id in enumerate(unknown_ids):
                    persist_online_install({
                        "id": install_id, "script_id": "demo", "running": False,
                        "status": "结果未知", "phase": "unknown",
                        "updated_at": f"2026-11-22 00:0{index}:00",
                    })
                before = ONLINE_INSTALL_JOBS_FILE.read_bytes()

                with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                    response = client.post(
                        "/api/online-scripts/install/demo",
                        json={"request_id": "c" * 32, "force": True}, headers={"X-Token": TOKEN},
                    )

            self.assertEqual(response.status_code, 409)
            self.assertFalse(response.json["retry_allowed"])
            self.assertEqual(response.json["request_id"], "c" * 32)
            start.assert_not_called()
            stored = json.loads(ONLINE_INSTALL_JOBS_FILE.read_text(encoding="utf-8"))
            self.assertEqual(set(stored), set(unknown_ids))
            self.assertEqual(ONLINE_INSTALL_JOBS_FILE.read_bytes(), before)

    def test_existing_running_install_can_reconcile_after_protected_history_overflow(self):
        with isolated_app() as (_app, _base_dir):
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_JOBS_FILE, get_online_install_job, persist_online_install

            install_ids = [f"{index + 1:032x}" for index in range(3)]
            with patch("fls_manager.online_scripts.constants.ONLINE_INSTALL_LIMIT", 3):
                for install_id in install_ids:
                    persist_online_install({
                        "id": install_id, "script_id": "demo", "running": True,
                        "status": "运行中", "phase": "running_command",
                        "updated_at": "2026-11-23 00:00:00",
                    })

            with patch("fls_manager.online_scripts.constants.ONLINE_INSTALL_LIMIT", 2):
                reconciled = get_online_install_job(install_ids[0])

            self.assertEqual(reconciled["status"], "结果未知")
            self.assertNotIn("recovery_persistence_error", reconciled)
            stored = json.loads(ONLINE_INSTALL_JOBS_FILE.read_text(encoding="utf-8"))
            self.assertEqual(len(stored), 3)
            self.assertEqual(stored[install_ids[0]]["status"], "结果未知")

    def test_api_install_start_receipt_published_before_error_still_starts_once(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import constants
            from fls_manager.online_scripts.source import save_online_script_cache

            item, _ = self.prepare(base_dir)
            save_online_script_cache([item])
            request_id = "8" * 32
            client = app.test_client()
            persist = constants.persist_online_install
            calls = 0

            def publish_then_raise(info):
                nonlocal calls
                result = persist(info)
                if info.get("id") == request_id and calls == 0:
                    calls += 1
                    raise OSError("install receipt acknowledgement lost")
                return result

            with patch(
                "fls_manager.routes.online_scripts.install.persist_online_install",
                side_effect=publish_then_raise,
            ), patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                response = client.post(
                    "/api/online-scripts/install/demo",
                    json={"request_id": request_id, "force": True}, headers={"X-Token": TOKEN},
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["request_id"], request_id)
            self.assertTrue(response.json["running"])
            self.assertEqual(response.json["phase"], "prepared")
            self.assertEqual(start.call_count, 1)
            persisted = constants._load_persisted_online_install(request_id)
            self.assertEqual(persisted["status"], "准备中")
            self.assertEqual(persisted["phase"], "prepared")

    def test_html_install_start_receipt_failure_has_same_request_recovery_form(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import constants
            from fls_manager.online_scripts.source import save_online_script_cache

            item, _ = self.prepare(base_dir)
            save_online_script_cache([item])
            request_id = "9" * 32
            client = app.test_client()
            persist = constants.persist_online_install
            fail_prepared = True

            def reject_prepared_once(info):
                nonlocal fail_prepared
                if info.get("id") == request_id and info.get("phase") == "prepared" and fail_prepared:
                    fail_prepared = False
                    raise OSError("install receipt disk full")
                return persist(info)

            with patch(
                "fls_manager.routes.online_scripts.install.persist_online_install",
                side_effect=reject_prepared_once,
            ), patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                failed = client.post(
                    "/online-scripts/install/demo",
                    data={"request_id": request_id, "force": "1"}, headers={"X-Token": TOKEN},
                )
            self.assertEqual(failed.status_code, 500)
            self.assertIn(
                f'action="/online-scripts/install-start-retry/{request_id}"',
                failed.get_data(as_text=True),
            )
            start.assert_not_called()

            log_page = client.get(f"/online-scripts/log/{request_id}", headers={"X-Token": TOKEN})
            self.assertEqual(log_page.status_code, 200)
            self.assertIn("沿原请求重试启动", log_page.get_data(as_text=True))
            self.assertIn(
                f'action="/online-scripts/install-start-retry/{request_id}"',
                log_page.get_data(as_text=True),
            )

            with patch(
                "fls_manager.routes.online_scripts.install.persist_online_install",
                wraps=constants.persist_online_install,
            ), patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                recovered = client.post(
                    f"/online-scripts/install-start-retry/{request_id}", headers={"X-Token": TOKEN},
                )
                replay = client.post(
                    "/api/online-scripts/install/demo",
                    json={"request_id": request_id, "force": True}, headers={"X-Token": TOKEN},
                )
            self.assertEqual(recovered.status_code, 302)
            self.assertEqual(recovered.headers["Location"].split("?")[0], f"/online-scripts/log/{request_id}")
            self.assertEqual(replay.status_code, 200)
            self.assertTrue(replay.json["replayed"])
            self.assertEqual(start.call_count, 1)
            self.assertTrue(constants._load_persisted_online_install(request_id)["running"])

    def test_html_unconfirmed_startup_receipt_offers_selection_not_dead_log_retry(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import constants
            from fls_manager.online_scripts.source import save_online_script_cache

            item, _ = self.prepare(base_dir)
            save_online_script_cache([item])
            request_id = "3" * 32
            client = app.test_client()
            with patch(
                "fls_manager.routes.online_scripts.install.persist_online_install",
                side_effect=OSError("install receipt storage unavailable"),
            ), patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                failed = client.post(
                    "/online-scripts/install/demo",
                    data={"request_id": request_id, "force": "1"}, headers={"X-Token": TOKEN},
                )

            html = failed.get_data(as_text=True)
            self.assertEqual(failed.status_code, 500)
            self.assertIn('href="/online-scripts/install-select/demo"', html)
            self.assertNotIn(f'action="/online-scripts/install-start-retry/{request_id}"', html)
            self.assertNotIn(f'href="/online-scripts/log/{request_id}"', html)
            self.assertIsNone(constants._load_persisted_online_install(request_id))
            start.assert_not_called()

    def test_api_install_assigns_id_and_replays_without_starting_twice(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import get_online_install_job

            self.prepare(base_dir)
            client = app.test_client()
            body = {"proxy_id": "", "import_task": True, "task_indexes": ["1"]}
            with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                first = client.post(
                    "/api/online-scripts/install/demo", json=body,
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(first.status_code, 200)
                payload = first.get_json()
                self.assertTrue(payload["ok"])
                self.assertRegex(payload["request_id"], r"^[a-f0-9]{32}$")
                self.assertEqual(payload["install_id"], payload["request_id"])

                repeated = client.post(
                    "/api/online-scripts/install/demo",
                    json={**body, "request_id": payload["request_id"]},
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(repeated.status_code, 200)
                self.assertTrue(repeated.get_json()["replayed"])
                self.assertEqual(repeated.get_json()["log_url"], payload["log_url"])
                start.assert_called_once()
            self.assertEqual(get_online_install_job(payload["install_id"])["selected_task_indexes"], ["1"])

    def test_api_install_replay_returns_original_job_when_script_cache_is_cleared(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_RUNNING, get_online_install_job
            from fls_manager.online_scripts.source import save_online_script_cache

            self.prepare(base_dir)
            client = app.test_client()
            request_id = "b" * 32
            body = {
                "request_id": request_id,
                "import_task": True,
                "select_mode": "all",
                "excluded_task_indexes": ["2"],
            }
            with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                first = client.post(
                    "/api/online-scripts/install/demo", json=body,
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(first.status_code, 200)
                self.assertEqual(first.json["install_id"], request_id)

                ONLINE_INSTALL_RUNNING.clear()
                save_online_script_cache([])
                repeated = client.post(
                    "/api/online-scripts/install/demo", json=body,
                    headers={"X-Token": TOKEN},
                )
                conflict = client.post(
                    "/api/online-scripts/install/demo",
                    json={**body, "excluded_task_indexes": ["1"]},
                    headers={"X-Token": TOKEN},
                )

            self.assertEqual(repeated.status_code, 200)
            self.assertTrue(repeated.json["replayed"])
            self.assertEqual(repeated.json["status"], "结果未知")
            self.assertFalse(repeated.json["running"])
            self.assertEqual(repeated.json["phase"], first.json["phase"])
            self.assertEqual(get_online_install_job(request_id)["selected_task_indexes"], ["1"])
            self.assertEqual(conflict.status_code, 409)
            self.assertEqual(conflict.json["install_id"], request_id)
            start.assert_called_once()

    def test_api_install_replay_survives_script_cache_read_error(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import get_online_install_job

            self.prepare(base_dir)
            client = app.test_client()
            request_id = "c" * 32
            body = {"request_id": request_id, "import_task": True, "task_indexes": ["2"]}
            with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                first = client.post(
                    "/api/online-scripts/install/demo", json=body,
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(first.status_code, 200)
                with patch(
                    "fls_manager.routes.online_scripts.install.get_online_script",
                    side_effect=RuntimeError("script cache unreadable"),
                ):
                    repeated = client.post(
                        "/api/online-scripts/install/demo", json=body,
                        headers={"X-Token": TOKEN},
                    )

            self.assertEqual(repeated.status_code, 200)
            self.assertTrue(repeated.json["replayed"])
            self.assertEqual(get_online_install_job(request_id)["selected_task_indexes"], ["2"])
            start.assert_called_once()

    def test_html_install_replay_returns_original_job_when_script_cache_is_cleared(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_RUNNING, get_online_install_job
            from fls_manager.online_scripts.source import save_online_script_cache

            self.prepare(base_dir)
            client = app.test_client()
            request_id = "a" * 32
            body = {
                "request_id": request_id,
                "import_task": "1",
                "select_mode": "all",
                "excluded_task_indexes": "2",
                "force": "1",
            }
            with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                first = client.post(
                    "/online-scripts/install/demo", data=body, headers={"X-Token": TOKEN},
                )
                self.assertEqual(first.status_code, 302)
                self.assertEqual(first.location.split("?", 1)[0], f"/online-scripts/log/{request_id}")

                ONLINE_INSTALL_RUNNING.clear()
                save_online_script_cache([])
                csrf = "form-replay-csrf"
                with client.session_transaction() as session:
                    session["csrf_token"] = csrf
                anonymous = client.post(
                    "/online-scripts/install/demo", data={**body, "csrf_token": csrf},
                )
                with client.session_transaction() as session:
                    session.update(
                        token=TOKEN,
                        token_expire_at=int(time.time()) + 3600,
                        security_verified=True,
                    )
                csrf_rejected = client.post("/online-scripts/install/demo", data=body)
                repeated = client.post(
                    "/online-scripts/install/demo", data={**body, "csrf_token": csrf},
                )
                conflict = client.post(
                    "/online-scripts/install/demo",
                    data={**body, "excluded_task_indexes": "1"}, headers={"X-Token": TOKEN},
                )

            self.assertEqual(anonymous.status_code, 302)
            self.assertIn("/login?next=", anonymous.location)
            self.assertEqual(csrf_rejected.status_code, 400)
            self.assertEqual(repeated.status_code, 302)
            self.assertEqual(repeated.location.split("?", 1)[0], f"/online-scripts/log/{request_id}")
            self.assertEqual(conflict.status_code, 409)
            self.assertIn(f"/online-scripts/log/{request_id}", conflict.get_data(as_text=True))
            self.assertEqual(get_online_install_job(request_id)["selected_task_indexes"], ["1"])
            start.assert_called_once()

    def test_html_install_replay_survives_script_cache_read_error(self):
        with isolated_app() as (app, base_dir):
            self.prepare(base_dir)
            client = app.test_client()
            request_id = "b" * 32
            body = {"request_id": request_id, "force": "1"}
            with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                first = client.post(
                    "/online-scripts/install/demo", data=body, headers={"X-Token": TOKEN},
                )
                self.assertEqual(first.status_code, 302)
                with patch(
                    "fls_manager.routes.online_scripts.install.get_online_script",
                    side_effect=RuntimeError("script cache unreadable"),
                ):
                    repeated = client.post(
                        "/online-scripts/install/demo", data=body, headers={"X-Token": TOKEN},
                    )

            self.assertEqual(repeated.status_code, 302)
            self.assertEqual(repeated.location.split("?", 1)[0], f"/online-scripts/log/{request_id}")
            start.assert_called_once()

    def test_html_startup_receipt_retry_still_requires_current_script_cache(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import constants

            self.prepare(base_dir)
            request_id = "c" * 32
            client = app.test_client()
            persist = constants.persist_online_install
            fail_prepared = True

            def reject_prepared_once(info):
                nonlocal fail_prepared
                if info.get("id") == request_id and info.get("phase") == "prepared" and fail_prepared:
                    fail_prepared = False
                    raise OSError("install receipt disk full")
                return persist(info)

            with patch(
                "fls_manager.routes.online_scripts.install.persist_online_install",
                side_effect=reject_prepared_once,
            ), patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                first = client.post(
                    "/online-scripts/install/demo",
                    data={"request_id": request_id, "force": "1"}, headers={"X-Token": TOKEN},
                )
                self.assertEqual(first.status_code, 500)
                with patch(
                    "fls_manager.routes.online_scripts.install.get_online_script",
                    side_effect=RuntimeError("script cache unreadable"),
                ):
                    retry = client.post(
                        "/online-scripts/install/demo",
                        data={"request_id": request_id, "force": "1"}, headers={"X-Token": TOKEN},
                    )

            self.assertEqual(retry.status_code, 500)
            self.assertIn("script cache unreadable", retry.get_data(as_text=True))
            self.assertEqual(constants._load_persisted_online_install(request_id)["phase"], "startup_receipt_failed")
            start.assert_not_called()

    def test_api_install_replay_normalizes_phase_against_persisted_status(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import (
                ONLINE_INSTALL_RUNNING, get_online_install_job, persist_online_install,
            )

            self.prepare(base_dir)
            client = app.test_client()
            request_id = "d" * 32
            body = {"request_id": request_id, "force": True}
            with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                first = client.post(
                    "/api/online-scripts/install/demo", json=body,
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(first.status_code, 200)
                info = get_online_install_job(request_id)
                info.update(running=False, status="结果未知", phase="completed", returncode=None)
                ONLINE_INSTALL_RUNNING[request_id].update(info)
                persist_online_install(info)

                repeated = client.post(
                    "/api/online-scripts/install/demo", json=body,
                    headers={"X-Token": TOKEN},
                )

            self.assertEqual(repeated.status_code, 200)
            self.assertTrue(repeated.json["replayed"])
            self.assertEqual(repeated.json["status"], "结果未知")
            self.assertEqual(repeated.json["phase"], "unknown")
            self.assertIn("原回执未修改", repeated.json["msg"])
            self.assertEqual(get_online_install_job(request_id)["phase"], "completed")
            start.assert_called_once()

    def test_api_install_option_conflict_and_invalid_selection_have_zero_side_effect(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import get_online_install_job

            self.prepare(base_dir)
            client = app.test_client()
            request_id = "d" * 32
            with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                first = client.post(
                    "/api/online-scripts/install/demo", json={
                        "request_id": request_id, "import_task": True, "task_indexes": ["1"],
                    }, headers={"X-Token": TOKEN},
                )
                self.assertEqual(first.status_code, 200)
                conflict = client.post(
                    "/api/online-scripts/install/demo", json={
                        "request_id": request_id, "import_task": True, "task_indexes": ["2"],
                    }, headers={"X-Token": TOKEN},
                )
                self.assertEqual(conflict.status_code, 409)
                invalid_replay = client.post(
                    "/api/online-scripts/install/demo", json={
                        "request_id": request_id, "import_task": True, "task_indexes": ["9"],
                    }, headers={"X-Token": TOKEN},
                )
                self.assertEqual(invalid_replay.status_code, 400)
                self.assertNotIn("accepted", invalid_replay.json)
                self.assertEqual(invalid_replay.json["install_id"], request_id)
                self.assertIn("/api/online-scripts/log/" + request_id, invalid_replay.json["log_url"])
                oversized_index = client.post(
                    "/api/online-scripts/install/demo", json={
                        "request_id": request_id, "import_task": True,
                        "task_indexes": ["9" * 5000],
                    }, headers={"X-Token": TOKEN},
                )
                self.assertEqual(oversized_index.status_code, 400)
                invalid_id = "e" * 32
                invalid = client.post(
                    "/api/online-scripts/install/demo", json={
                        "request_id": invalid_id, "import_task": True, "task_indexes": ["9"],
                    }, headers={"X-Token": TOKEN},
                )
                self.assertEqual(invalid.status_code, 400)
                self.assertIs(invalid.json["accepted"], False)
                excluded = client.post(
                    "/api/online-scripts/install/demo", json={
                        "request_id": "f" * 32, "import_task": True,
                        "select_mode": "all", "excluded_task_indexes": ["9"],
                    }, headers={"X-Token": TOKEN},
                )
                self.assertEqual(excluded.status_code, 400)
                self.assertIs(excluded.json["accepted"], False)
                mixed = client.post(
                    "/api/online-scripts/install/demo", json={
                        "request_id": "a" * 32, "task_indexes": ["1"],
                    }, headers={"X-Token": TOKEN},
                )
                self.assertEqual(mixed.status_code, 400)
                self.assertIs(mixed.json["accepted"], False)
                invalid_proxy = client.post(
                    "/api/online-scripts/install/demo", json={
                        "request_id": "9" * 32, "proxy_id": "missing-proxy",
                    }, headers={"X-Token": TOKEN},
                )
                self.assertEqual(invalid_proxy.status_code, 400)
                self.assertIs(invalid_proxy.json["accepted"], False)
                start.assert_called_once()
            self.assertIsNone(get_online_install_job(invalid_id))
            self.assertIsNone(get_online_install_job("f" * 32))
            self.assertIsNone(get_online_install_job("a" * 32))

    def test_api_install_existing_target_requires_confirmation_then_force_uses_same_id(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import get_online_install_job

            self.prepare(base_dir)
            target = base_dir / "scripts" / "demo.sh"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("original", encoding="utf-8")
            client = app.test_client()
            request_id = "1" * 32
            with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                confirmation = client.post(
                    "/api/online-scripts/install/demo", json={"request_id": request_id},
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(confirmation.status_code, 409)
                self.assertTrue(confirmation.get_json()["confirm_required"])
                self.assertTrue(confirmation.get_json()["target_exists"])
                self.assertNotIn("target", confirmation.get_json())
                self.assertNotIn(str(target), confirmation.get_data(as_text=True))
                self.assertIsNone(get_online_install_job(request_id))
                forced = client.post(
                    "/api/online-scripts/install/demo",
                    json={"request_id": request_id, "force": True},
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(forced.status_code, 200)
                self.assertEqual(forced.get_json()["request_id"], request_id)
                start.assert_called_once()
            self.assertEqual(get_online_install_job(request_id)["force"], True)
            self.assertEqual(target.read_text(encoding="utf-8"), "original")

    def test_api_install_start_failure_is_queryable_without_retrying(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import get_online_install_job

            self.prepare(base_dir)
            client = app.test_client()
            request_id = "2" * 32
            with patch(
                "fls_manager.routes.online_scripts.install.start_install_thread",
                side_effect=RuntimeError("thread unavailable"),
            ) as start:
                first = client.post(
                    "/api/online-scripts/install/demo",
                    json={"request_id": request_id}, headers={"X-Token": TOKEN},
                )
                self.assertEqual(first.status_code, 500)
                self.assertEqual(first.get_json()["status"], "启动失败")
                self.assertIn("thread unavailable", first.get_json()["msg"])
                repeated = client.post(
                    "/api/online-scripts/install/demo",
                    json={"request_id": request_id}, headers={"X-Token": TOKEN},
                )
                self.assertEqual(repeated.status_code, 200)
                self.assertTrue(repeated.get_json()["replayed"])
                start.assert_called_once()
            self.assertEqual(get_online_install_job(request_id)["status"], "启动失败")

    def test_api_thread_start_failure_with_unconfirmed_receipt_can_retry_same_request(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import constants
            from fls_manager.online_scripts.source import save_online_script_cache

            item, _ = self.prepare(base_dir)
            save_online_script_cache([item])
            request_id = "5" * 32
            client = app.test_client()
            persist = constants.persist_online_install

            def reject_start_failure_receipts(info):
                if info.get("id") == request_id and info.get("phase") in {
                    "startup_failed", "startup_receipt_failed",
                }:
                    raise OSError("install history temporarily read-only")
                return persist(info)

            body = {"request_id": request_id, "force": True}
            with patch(
                "fls_manager.routes.online_scripts.install.persist_online_install",
                side_effect=reject_start_failure_receipts,
            ), patch(
                "fls_manager.routes.online_scripts.install.start_install_thread",
                side_effect=[RuntimeError("thread quota exhausted"), None],
            ) as start:
                failed = client.post(
                    "/api/online-scripts/install/demo", json=body, headers={"X-Token": TOKEN},
                )

                self.assertEqual(failed.status_code, 500)
                self.assertFalse(failed.json["accepted"])
                self.assertTrue(failed.json["retry_allowed"])
                self.assertEqual(failed.json["request_id"], request_id)
                self.assertEqual(failed.json["phase"], "startup_receipt_failed")
                self.assertNotIn("log_url", failed.json)
                self.assertEqual(
                    constants._load_persisted_online_install(request_id)["phase"], "prepared",
                )

                state = client.get(
                    f"/api/online-scripts/log/{request_id}?state_only=1", headers={"X-Token": TOKEN},
                )
                self.assertEqual(state.status_code, 200)
                self.assertTrue(state.json["recovery"]["available"])

                with patch(
                    "fls_manager.routes.online_scripts.install.persist_online_install",
                    wraps=persist,
                ):
                    retried = client.post(
                        "/api/online-scripts/install/demo", json=body, headers={"X-Token": TOKEN},
                    )

                self.assertEqual(retried.status_code, 200)
                self.assertTrue(retried.json["ok"])
                self.assertEqual(retried.json["request_id"], request_id)
                self.assertFalse(retried.json.get("replayed", False))
                self.assertEqual(start.call_count, 2)

                replayed = client.post(
                    "/api/online-scripts/install/demo", json=body, headers={"X-Token": TOKEN},
                )
                self.assertEqual(replayed.status_code, 200)
                self.assertTrue(replayed.json["replayed"])
                self.assertEqual(start.call_count, 2)

    def test_api_thread_start_failure_receipt_published_before_error_is_confirmed(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import constants
            from fls_manager.online_scripts.source import save_online_script_cache

            item, _ = self.prepare(base_dir)
            save_online_script_cache([item])
            request_id = "6" * 32
            persist = constants.persist_online_install

            def publish_then_raise(info):
                if info.get("id") == request_id and info.get("phase") == "startup_failed":
                    persist(info)
                    raise OSError("startup failure acknowledgement lost")
                return persist(info)

            with patch(
                "fls_manager.routes.online_scripts.install.persist_online_install",
                side_effect=publish_then_raise,
            ), patch(
                "fls_manager.routes.online_scripts.install.start_install_thread",
                side_effect=RuntimeError("thread quota exhausted"),
            ) as start:
                failed = app.test_client().post(
                    "/api/online-scripts/install/demo",
                    json={"request_id": request_id, "force": True}, headers={"X-Token": TOKEN},
                )

            self.assertEqual(failed.status_code, 500)
            self.assertEqual(failed.json["phase"], "startup_failed")
            self.assertNotIn("accepted", failed.json)
            self.assertEqual(failed.json["log_url"], f"/api/online-scripts/log/{request_id}")
            persisted = constants._load_persisted_online_install(request_id)
            self.assertEqual(persisted["phase"], "startup_failed")
            self.assertEqual(persisted["status"], "启动失败")
            state = app.test_client().get(
                f"/api/online-scripts/log/{request_id}?state_only=1", headers={"X-Token": TOKEN},
            )
            self.assertEqual(state.status_code, 200)
            self.assertEqual(state.json["phase"], "startup_failed")
            self.assertTrue(state.json["recovery"]["available"])
            start.assert_called_once()

    def test_html_thread_start_failure_receipt_failure_offers_original_request_recovery(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import constants
            from fls_manager.online_scripts.source import save_online_script_cache

            item, _ = self.prepare(base_dir)
            save_online_script_cache([item])
            request_id = "7" * 32
            persist = constants.persist_online_install

            def reject_failure_receipts(info):
                if info.get("id") == request_id and info.get("phase") in {
                    "startup_failed", "startup_receipt_failed",
                }:
                    raise OSError("install history temporarily read-only")
                return persist(info)

            client = app.test_client()
            with patch(
                "fls_manager.routes.online_scripts.install.persist_online_install",
                side_effect=reject_failure_receipts,
            ), patch(
                "fls_manager.routes.online_scripts.install.start_install_thread",
                side_effect=RuntimeError("thread quota exhausted"),
            ) as start:
                failed = client.post(
                    "/online-scripts/install/demo",
                    data={"request_id": request_id, "force": "1"}, headers={"X-Token": TOKEN},
                )

            html = failed.get_data(as_text=True)
            self.assertEqual(failed.status_code, 500)
            self.assertIn(
                f'action="/online-scripts/install-start-retry/{request_id}"', html,
            )
            self.assertIn("沿原请求重试启动", html)
            self.assertNotIn(f'href="/online-scripts/log/{request_id}"', html)
            self.assertEqual(
                constants._load_persisted_online_install(request_id)["phase"], "prepared",
            )
            start.assert_called_once()

    def test_api_install_requires_csrf_and_live_session_without_mutating_state(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import get_online_install_job

            self.prepare(base_dir)
            client = app.test_client()
            with client.session_transaction() as session:
                session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                               security_verified=True, csrf_token="api-install-csrf")
            with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                denied = client.post(
                    "/api/online-scripts/install/demo", json={"request_id": "3" * 32},
                )
                self.assertEqual(denied.status_code, 400)
                self.assertIn("CSRF", denied.get_json()["msg"])
                self.assertIsNone(get_online_install_job("3" * 32))
                forbidden = client.post(
                    "/api/online-scripts/install/demo?token=wrong",
                    json={"request_id": "4" * 32},
                    headers={"X-CSRF-Token": "api-install-csrf"},
                )
                self.assertEqual(forbidden.status_code, 403)
                self.assertIsNone(get_online_install_job("4" * 32))
                with client.session_transaction() as session:
                    session["token_expire_at"] = 1
                expired = client.post(
                    "/api/online-scripts/install/demo", json={"request_id": "5" * 32},
                    headers={"X-CSRF-Token": "api-install-csrf"},
                )
                self.assertEqual(expired.status_code, 401)
                start.assert_not_called()

    def test_api_task_retry_reuses_downloaded_target_without_reinstalling(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import get_online_install_job, persist_online_install
            from fls_manager.online_scripts.install import _fingerprint_path
            from fls_manager.online_scripts.logs import online_install_log_file

            item, info = self.prepare(base_dir)
            info["id"] = "a" * 32
            target = base_dir / "scripts" / "demo.sh"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("downloaded", encoding="utf-8")
            info.update(
                running=False,
                status="失败",
                phase="importing_tasks",
                target_path=str(target),
                target_fingerprint=_fingerprint_path(target),
                log_file=str(online_install_log_file(info["id"], item["name"])),
                error="任务导入失败",
            )
            persist_online_install(info)
            client = app.test_client()
            with patch("fls_manager.routes.online_scripts.install.import_task_if_needed", return_value=(True, "recovered")) as importer, patch(
                "fls_manager.online_scripts.install.download_online_script_logged"
            ) as download:
                recovered = client.post(
                    "/api/online-scripts/install-task-retry/" + info["id"],
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(recovered.status_code, 200)
                self.assertTrue(recovered.get_json()["ok"])
                self.assertEqual(recovered.get_json()["phase"], "completed")
                importer.assert_called_once()
                download.assert_not_called()
                repeated = client.post(
                    "/api/online-scripts/install-task-retry/" + info["id"],
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(repeated.status_code, 409)
                importer.assert_called_once()
            completed_job = get_online_install_job(info["id"])
            self.assertEqual(completed_job["status"], "已完成")
            self.assertEqual(completed_job.get("error"), "")
            self.assertEqual(completed_job.get("scheduler_reload_error"), "")

    def test_task_retry_scheduler_failure_records_outputs_for_reload_recovery(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.models import load_tasks
            from fls_manager.online_scripts.constants import get_online_install_job
            from fls_manager.online_scripts.install import capture_install_outputs
            from fls_manager.online_scripts.logs import online_install_log_file

            _item, info = self.prepare(base_dir)
            info.update(
                id="c" * 32,
                running=False,
                status="失败",
                phase="importing_tasks",
            )
            target = base_dir / "scripts" / "demo.sh"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("downloaded", encoding="utf-8")
            log_path = online_install_log_file(info["id"], info["script_name"])
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("download completed\n", encoding="utf-8")
            info["log_file"] = str(log_path)
            capture_install_outputs(info, target)
            client = app.test_client()
            endpoint = f"/api/online-scripts/install-task-retry/{info['id']}"

            with patch(
                "fls_manager.online_scripts.tasks.reload_scheduler",
                side_effect=RuntimeError("scheduler unavailable"),
            ) as failed_reload, patch(
                "fls_manager.online_scripts.install.download_online_script_logged",
            ) as download:
                failed = client.post(endpoint, headers={"X-Token": TOKEN})
            self.assertEqual(failed.status_code, 503)
            self.assertTrue(failed.json["scheduler_reload_required"])
            failed_reload.assert_called_once()
            download.assert_not_called()
            self.assertEqual([task["name"] for task in load_tasks()], ["Hourly"])

            with patch(
                "fls_manager.routes.online_scripts.install.reload_scheduler",
            ) as recovered_reload, patch(
                "fls_manager.online_scripts.install.download_online_script_logged",
            ) as download:
                recovered = client.post(
                    f"/api/online-scripts/install-reload/{info['id']}",
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(recovered.status_code, 200)
            self.assertTrue(recovered.json["ok"])
            recovered_reload.assert_called_once()
            download.assert_not_called()
            self.assertEqual(get_online_install_job(info["id"])["status"], "已完成")
            self.assertEqual([task["name"] for task in load_tasks()], ["Hourly"])

            final_state = client.get(
                f"/api/online-scripts/log/{info['id']}", headers={"X-Token": TOKEN},
            ).json
            self.assertFalse(final_state["consistency"]["changed"])
            self.assertEqual(final_state["consistency"]["tasks"], "unchanged")

    def test_api_task_retry_refuses_later_target_changes_and_requires_csrf(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import persist_online_install
            from fls_manager.online_scripts.install import _fingerprint_path
            from fls_manager.online_scripts.logs import online_install_log_file

            _item, info = self.prepare(base_dir)
            info["id"] = "b" * 32
            target = base_dir / "scripts" / "demo.sh"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("downloaded", encoding="utf-8")
            info.update(running=False, status="失败", phase="importing_tasks", target_path=str(target), target_fingerprint=_fingerprint_path(target))
            info["log_file"] = str(online_install_log_file(info["id"], _item["name"]))
            persist_online_install(info)
            client = app.test_client()
            denied = client.post("/api/online-scripts/install-task-retry/" + info["id"])
            self.assertEqual(denied.status_code, 400)
            target.write_text("edited later", encoding="utf-8")
            with patch("fls_manager.routes.online_scripts.install.import_task_if_needed") as importer:
                changed = client.post(
                    "/api/online-scripts/install-task-retry/" + info["id"],
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(changed.status_code, 409)
            self.assertIn("后来修改", changed.get_json()["msg"])
            importer.assert_not_called()
            payload = client.get(
                "/api/online-scripts/log/" + info["id"], headers={"X-Token": TOKEN},
            ).get_json()
            self.assertFalse(payload["recovery"]["available"])
            self.assertIn("后来修改", payload["recovery"]["reason"])

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise install selection AJAX")
    def test_install_selection_ajax_handles_confirmation_and_redirects_to_original_log(self):
        with isolated_app() as (app, base_dir):
            self.prepare(base_dir)
            client = app.test_client()
            html = client.get("/online-scripts/install-select/demo", headers={"X-Token": TOKEN}).get_data(as_text=True)
            script = next(script for script in re.findall(r"<script>(.*?)</script>", html, re.S) if "flsInstallSubmitApi" in script)
            source = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
            reader = source[source.index("async function flsReadJsonResponse("):source.index("async function flsReadTextResponse(")]
            result = subprocess.run(["node", "-e", r"""
const vm=require('node:vm');const assert=require('node:assert/strict');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const request={value:'a'.repeat(32)}; const importInput={checked:true};
const storage={value:null,getItem(){return this.value;},setItem(_key,value){this.value=value;},removeItem(){this.value=null;}};
const form={dataset:{},listeners:{},querySelector:s=>s.includes('request_id')?request:s.includes('import_task')?importInput:null,
 getAttribute:()=>'/online-scripts/install/demo', querySelectorAll:()=>[], addEventListener(n,f){this.listeners[n]=f;}};
const status={hidden:true,textContent:''},login={hidden:true,href:''},excluded={value:''}; let calls=[];
class FormDataMock{constructor(){this.values={request_id:request.value};}delete(k){delete this.values[k];}set(k,v){this.values[k]=v;}}
    const context={window:{FLS_INSTALL_SCRIPT_ID:'demo',confirm:()=>true,location:{pathname:'/online-scripts/install-select/demo',search:''}},
 document:{getElementById:id=>id==='onlineInstallSelectForm'?form:id==='onlineInstallApiError'?status:id==='excludedTaskIndexes'?excluded:login,
  querySelector:()=>null,querySelectorAll:()=>[]},
     FormData:FormDataMock,sessionStorage:storage,confirm:()=>true,
 flsGetCsrfToken:()=> 'install-csrf', flsReadJsonResponse:async r=>{if(!r.ok){const e=new Error(r.jsonData.msg);e.status=r.status;e.payload=r.jsonData;throw e;}return r.jsonData;},
 fetch:async(url,opts)=>{calls.push({url,opts});const value=calls.length===1?{confirm_required:true,msg:'target exists'}:{ok:true,request_id:'a'.repeat(32),install_id:'a'.repeat(32)};return {ok:calls.length!==1,status:calls.length===1?409:200,json:async()=>value};},
 encodeURIComponent,alert:()=>{}
    };vm.createContext(context);vm.runInContext(input.reader+input.script,context);
(async()=>{await context.flsInstallSubmitApi('/online-scripts/install/demo',form,{disabled:false});
 assert.equal(calls.length,2);assert.equal(calls[0].url,'/api/online-scripts/install/demo');assert.equal(calls[1].opts.body.values.force,'1');
 assert.equal(calls[0].opts.headers['X-CSRF-Token'],'install-csrf');assert.equal(context.window.location.href,'/online-scripts/log/'+ 'a'.repeat(32));})().catch(e=>{console.error(e);process.exitCode=1;});
"""], input=json.dumps({"script":script,"reader":reader}), capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise rejected install recovery")
    def test_install_selection_ajax_unlocks_after_explicit_preacceptance_rejection(self):
        with isolated_app() as (app, base_dir):
            self.prepare(base_dir)
            html = app.test_client().get(
                "/online-scripts/install-select/demo", headers={"X-Token": TOKEN},
            ).get_data(as_text=True)
            script = next(
                script for script in re.findall(r"<script>(.*?)</script>", html, re.S)
                if "flsInstallSubmitApi" in script
            )
            source = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
            reader = source[
                source.index("async function flsReadJsonResponse("):source.index(
                    "async function flsReadTextResponse("
                )
            ]
            result = subprocess.run(["node", "-e", r"""
const vm=require('node:vm');const assert=require('node:assert/strict');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const id='e'.repeat(32),request={value:id},proxy={value:'missing-proxy'};
const button={disabled:false};
const ids=['onlineInstallApiError','onlineInstallApiLogin','onlineInstallRecoveryHistory',
  'onlineInstallRecoveryInstallHistory','onlineInstallRecoveryCheckHistory','onlineInstallRecoveryClear',
  'onlineInstallRecoveryStatus','onlineInstallRecoveryLog','onlineInstallRecoveryStatusLink',
  'onlineInstallRecoveryReload','onlineInstallRecoveryConfirm','onlineInstallRecoveryRetry',
  'onlineInstallRecoveryResume','onlineInstallRecoveryHistoryItems'];
const elements=Object.fromEntries(ids.map(name=>[name,{hidden:true,disabled:false,textContent:'',href:'',onclick:null,
  replaceChildren(){},appendChild(){}}]));
const form={__flsInstallRecoveryLocked:false,__flsInstallSubmitting:false,
  querySelector(selector){return selector.includes('request_id')?request:selector.includes('proxy_id')?proxy:null;},
  querySelectorAll(){return[button];},getAttribute(){return'/online-scripts/install/demo';},addEventListener(){}};
const storage={value:null,getItem(){return this.value;},setItem(_key,value){this.value=value;},removeItem(){this.value=null;}};
class FormDataMock{constructor(){this.values={request_id:id,proxy_id:proxy.value};}delete(key){delete this.values[key];}set(key,value){this.values[key]=value;}}
const calls=[];
const context={window:{FLS_INSTALL_SCRIPT_ID:'demo',location:{pathname:'/online-scripts/install-select/demo',search:''}},
  location:{pathname:'/online-scripts/install-select/demo',search:''},document:{
    getElementById(name){return name==='onlineInstallSelectForm'?form:name==='excludedTaskIndexes'?{value:''}:elements[name];},
    querySelectorAll(){return[];},querySelector(){return null;},createElement(){return{textContent:'',href:'',appendChild(){}};}},
  sessionStorage:storage,FormData:FormDataMock,flsGetCsrfToken:()=> 'csrf',flsApplyExcludedToVisible(){},
  flsResponseErrorDiagnostic(_response,body){const payload=JSON.parse(body);return{payload,message:payload.msg};},
  fetch:async(url,options)=>{calls.push({url,options});return{ok:false,status:400,
    headers:{get:()=> 'application/json'},text:async()=>JSON.stringify({ok:false,accepted:false,
      request_id:id,retry_allowed:true,msg:'代理选择无效'})};},encodeURIComponent,setTimeout,confirm:()=>true};
vm.createContext(context);vm.runInContext(input.reader+input.script,context);
(async()=>{
  await context.flsInstallSubmitApi('/online-scripts/install/demo',form,button);
  assert.equal(calls.length,1);assert.equal(calls[0].url,'/api/online-scripts/install/demo');
  assert.equal(calls[0].options.headers['X-CSRF-Token'],'csrf');
  assert.equal(storage.value,null);assert.equal(form.__flsInstallRecoveryLocked,false);
  assert.equal(form.__flsInstallSubmitting,false);assert.equal(button.disabled,false);
  assert.equal(request.value,id);assert.equal(proxy.value,'missing-proxy');
  assert.match(elements.onlineInstallApiError.textContent,/代理选择无效/);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""], input=json.dumps({"script": script, "reader": reader}), capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise ambiguous install recovery")
    def test_install_selection_ambiguous_submit_reconciles_original_request_without_repost(self):
        with isolated_app() as (app, base_dir):
            self.prepare(base_dir)
            html = app.test_client().get(
                "/online-scripts/install-select/demo", headers={"X-Token": TOKEN},
            ).get_data(as_text=True)
            script = next(
                script for script in re.findall(r"<script>(.*?)</script>", html, re.S)
                if "flsInstallSubmitApi" in script
            )
            source = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
            reader = source[
                source.index("async function flsReadJsonResponse("):source.index(
                    "async function flsReadTextResponse("
                )
            ]
            result = subprocess.run(["node", "-e", r"""
const vm=require('node:vm'),assert=require('node:assert/strict');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const id='d'.repeat(32);
function setup(responses){
  const ids=['onlineInstallApiError','onlineInstallApiLogin','onlineInstallRecoveryStatus',
    'onlineInstallRecoveryLog','onlineInstallRecoveryStatusLink','onlineInstallRecoveryHistory',
    'onlineInstallRecoveryConfirm','onlineInstallRecoveryReload','onlineInstallRecoveryRetry',
    'onlineInstallRecoveryResume','onlineInstallRecoveryInstallHistory','onlineInstallRecoveryCheckHistory',
    'onlineInstallRecoveryHistoryItems','onlineInstallRecoveryClear'];
  const elements=Object.fromEntries(ids.map(name=>[name,{hidden:true,disabled:false,textContent:'',href:'',onclick:null,
    replaceChildren(){},appendChild(){}}]));
  const request={value:id},importTask={checked:false},enableTask={checked:true},proxy={value:''};
  const button={disabled:false};
  const form={__flsInstallRecoveryLocked:false,__flsInstallSubmitting:false,addEventListener(){},
    getAttribute(){return '/online-scripts/install/demo';},
    querySelector(selector){
      if(selector.includes('request_id'))return request;
      if(selector.includes('import_task'))return importTask;
      if(selector.includes('enable_task'))return enableTask;
      if(selector.includes('proxy_id'))return proxy;
      return null;
    },querySelectorAll(){return[button];}};
  const storage={value:null,getItem(){return this.value;},setItem(_key,value){this.value=value;},removeItem(){this.value=null;}};
  const calls=[];
  class FormDataMock{constructor(){this.values={request_id:id};}delete(name){delete this.values[name];}set(name,value){this.values[name]=value;}}
  const context={window:{FLS_INSTALL_SCRIPT_ID:'demo',confirm:()=>true,
      location:{pathname:'/online-scripts/install-select/demo',search:''}},
    location:{pathname:'/online-scripts/install-select/demo',search:''},document:{
      getElementById(name){return name==='onlineInstallSelectForm'?form:name==='excludedTaskIndexes'?{value:''}:elements[name];},
      querySelectorAll(){return[];},querySelector(){return null;},createElement(){return{textContent:'',href:'',appendChild(){}};}},
    sessionStorage:storage,FormData:FormDataMock,flsGetCsrfToken:()=> 'csrf',flsApplyExcludedToVisible(){},
    flsResponseErrorDiagnostic(_response,body){let payload=null;try{payload=JSON.parse(body)}catch(_){};return{payload,message:payload&&payload.msg||''};},
    fetch:async(url,options)=>{calls.push({url,options});const item=responses.shift();if(item.throw)throw new Error(item.throw);
      return{ok:item.ok,status:item.status,headers:{get:()=> 'application/json'},text:async()=>item.body};},
    encodeURIComponent,setTimeout};
  vm.createContext(context);vm.runInContext(input.reader+input.script,context);
  return{context,elements,form,button,storage,calls};
}
const reply=(status,payload)=>({ok:status>=200&&status<300,status,body:JSON.stringify(payload)});
(async()=>{
  const running=setup([
    reply(502,{msg:'gateway lost response'}),
    reply(200,{ok:true,install_id:id,running:true,status:'安装中',phase:'downloading'})
  ]);
  await new Promise(resolve=>setImmediate(resolve));
  await running.context.flsInstallSubmitApi('/online-scripts/install/demo',running.form,running.button);
  assert.equal(running.calls.length,2);
  assert.equal(running.calls[0].url,'/api/online-scripts/install/demo');
  assert.equal(running.calls[0].options.method,'POST');
  assert.equal(running.calls[1].url,'/api/online-scripts/log/'+id+'?state_only=1');
  assert.equal(running.calls[1].options.method,undefined);
  assert.equal(running.storage.value,JSON.stringify({request_id:id,script_id:'demo',action:'/online-scripts/install/demo',
    import_task:false,excluded_task_indexes:'',enable_task:true,proxy_id:'',force:false}));
  assert.equal(running.form.__flsInstallRecoveryLocked,true);
  assert.equal(running.elements.onlineInstallRecoveryLog.href,'/online-scripts/log/'+id);
  assert.match(running.elements.onlineInstallRecoveryStatus.textContent,/安装中/);

  const retry=setup([reply(502,{msg:'gateway lost response'}),reply(503,{msg:'status store unavailable'}),
    reply(200,{ok:true,install_id:id,running:false,status:'启动失败',phase:'startup_failed'})]);
  await new Promise(resolve=>setImmediate(resolve));
  await retry.context.flsInstallSubmitApi('/online-scripts/install/demo',retry.form,retry.button);
  assert.equal(retry.calls.length,2);
  assert.match(retry.elements.onlineInstallRecoveryStatus.textContent,/status store unavailable/);
  assert.equal(retry.elements.onlineInstallRecoveryRetry.hidden,false);
  await retry.elements.onlineInstallRecoveryRetry.onclick();
  assert.equal(retry.calls.length,3);
  assert.equal(retry.calls[2].url,'/api/online-scripts/log/'+id+'?state_only=1');
  assert.equal(retry.calls[2].options.method,undefined);
  assert.equal(retry.storage.value!==null,true);

  const denied=setup([reply(403,{msg:'permission expired'})]);
  await new Promise(resolve=>setImmediate(resolve));
  await denied.context.flsInstallSubmitApi('/online-scripts/install/demo',denied.form,denied.button);
  assert.equal(denied.calls.length,1);
  assert.equal(denied.elements.onlineInstallApiLogin.hidden,false);
  assert.equal(denied.storage.value!==null,true);

  const acceptedReadFailure=setup([
    reply(503,{ok:false,accepted:true,request_id:id,install_id:id,retry_allowed:false,
      log_url:'/api/online-scripts/log/'+id,state_url:'/api/online-scripts/log/'+id+'?state_only=1',
      msg:'安装已受理，但当前状态读取失败'}),
    reply(200,{ok:true,request_id:id,install_id:id,running:true,status:'运行中',phase:'downloading'})
  ]);
  await new Promise(resolve=>setImmediate(resolve));
  await acceptedReadFailure.context.flsInstallSubmitApi(
    '/online-scripts/install/demo',acceptedReadFailure.form,acceptedReadFailure.button);
  assert.equal(acceptedReadFailure.calls.length,2,'accepted requests reconcile via GET, never repost');
  assert.equal(acceptedReadFailure.calls[0].options.method,'POST');
  assert.equal(acceptedReadFailure.calls[1].url,'/api/online-scripts/log/'+id+'?state_only=1');
  assert.equal(acceptedReadFailure.calls[1].options.method,undefined);
  assert.equal(acceptedReadFailure.storage.value!==null,true);
  assert.equal(acceptedReadFailure.form.__flsInstallRecoveryLocked,true);
  assert.equal(acceptedReadFailure.elements.onlineInstallRecoveryLog.href,'/online-scripts/log/'+id);
  assert.match(acceptedReadFailure.elements.onlineInstallRecoveryStatus.textContent,/运行中/);

  const full=setup([reply(409,{ok:false,accepted:false,history_full:true,request_id:id,
    msg:'待核对在线安装占满历史容量',history_url:'/online-scripts/install-history',retry_allowed:false})]);
  await new Promise(resolve=>setImmediate(resolve));
  await full.context.flsInstallSubmitApi('/online-scripts/install/demo',full.form,full.button);
  assert.equal(full.calls.length,1,'a known rejected install must not query or repost');
  assert.equal(full.storage.value,null,'the rejected request is not an accepted job');
  assert.equal(full.form.__flsInstallRecoveryLocked,false);
  assert.equal(full.button.disabled,false);
  assert.equal(full.elements.onlineInstallRecoveryInstallHistory.hidden,false);
  assert.equal(full.elements.onlineInstallRecoveryInstallHistory.href,'/online-scripts/install-history');
  assert.match(full.elements.onlineInstallRecoveryStatus.textContent,/新请求未启动/);
  assert.match(full.elements.onlineInstallApiError.textContent,/安装未启动/);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""], input=json.dumps({"script": script, "reader": reader}), capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise install recovery")
    def test_install_selection_refresh_recovers_original_request_without_new_post(self):
        with isolated_app() as (app, base_dir):
            self.prepare(base_dir)
            html = app.test_client().get(
                "/online-scripts/install-select/demo", headers={"X-Token": TOKEN},
            ).get_data(as_text=True)
            script = next(
                script for script in re.findall(r"<script>(.*?)</script>", html, re.S)
                if "flsInstallRecoverPending" in script
            )
            source = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
            reader = source[
                source.index("async function flsReadJsonResponse("):source.index(
                    "async function flsReadTextResponse("
                )
            ]
            result = subprocess.run(
                ["node", "-e", r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const installId = 'a'.repeat(32);

async function runCase(pending, response, expected) {
  const elements = Object.fromEntries([
    'onlineInstallRecoveryStatus', 'onlineInstallRecoveryLog',
    'onlineInstallRecoveryRetry', 'onlineInstallApiLogin',
    'onlineInstallRecoveryHistory', 'onlineInstallRecoveryInstallHistory',
    'onlineInstallRecoveryCheckHistory', 'onlineInstallRecoveryHistoryItems',
    'onlineInstallRecoveryClear', 'onlineInstallRecoveryResume',
  ].map(id => [id, {hidden: true, disabled: false, textContent: '', href: '', onclick: null}]));
  elements.onlineInstallRecoveryHistoryItems.replaceChildren = function() {};
  elements.onlineInstallRecoveryHistoryItems.appendChild = function() {};
  const calls = [];
      const storage = {
        value: pending === undefined ? null : JSON.stringify(pending),
        getItem() { return this.value; },
        setItem(_key, value) { this.value = value; },
      };
      const requestField = {value: 'c'.repeat(32)};
      const form = {addEventListener() {}, querySelector: s => s.includes('request_id') ? requestField : null, querySelectorAll: () => []};
      const context = {
    window: {FLS_INSTALL_SCRIPT_ID: 'demo', location: {pathname: '/online-scripts/install-select/demo', search: '', reload() {}}},
    location: {pathname: '/online-scripts/install-select/demo', search: ''},
    confirm: () => true,
        document: {
          getElementById: id => id === 'onlineInstallSelectForm' ? form : elements[id],
      querySelectorAll: () => [],
      querySelector: () => null,
    },
        sessionStorage: storage,
        flsResponseErrorDiagnostic: (response, body) => {
          let payload = null;
          try { payload = JSON.parse(body); } catch (_) {}
          return {payload, message: payload && (payload.msg || payload.error || payload.message) || ''};
        },
        fetch: async (url, options) => {
      calls.push({url, options});
      if (response.throw) throw new Error(response.throw);
      return {
        ok: response.ok,
        status: response.status,
        text: async () => response.body,
      };
    },
    encodeURIComponent,
    flsApplyExcludedToVisible: () => {},
    setTimeout,
  };
  vm.createContext(context);
  vm.runInContext(input.reader + input.script, context);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls.length, expected.calls);
  if (expected.calls) {
    assert.equal(calls[0].url, '/api/online-scripts/log/' + installId + '?state_only=1');
    assert.equal(calls[0].options.method, undefined);
    assert.equal(calls[0].options.credentials, 'same-origin');
  }
  assert.equal(elements.onlineInstallRecoveryLog.hidden, expected.logHidden);
      assert.equal(elements.onlineInstallRecoveryRetry.hidden, expected.retryHidden);
      assert.equal(elements.onlineInstallApiLogin.hidden, expected.loginHidden);
      assert.match(elements.onlineInstallRecoveryStatus.textContent, expected.status);
      assert.equal(form.__flsInstallRecoveryLocked, expected.locked);
      assert.equal(elements.onlineInstallRecoveryCheckHistory.hidden, expected.historyHidden);
      assert.equal(storage.value, pending === undefined ? null : JSON.stringify(pending));
      if (expected.logHidden === false) {
        assert.equal(elements.onlineInstallRecoveryLog.href, '/online-scripts/log/' + installId);
      }
      if (expected.retry) {
        await elements.onlineInstallRecoveryRetry.onclick();
        assert.equal(calls.length, expected.calls + 1);
        assert.equal(calls[1].url, calls[0].url);
        assert.equal(calls[1].options.method, undefined);
      }
}

(async () => {
  await runCase(
    {install_id: installId, script_id: 'demo'},
    {ok: true, status: 200, body: JSON.stringify({ok:true,install_id:installId,running: true, status: '安装中', phase: 'downloading'})},
        {calls: 1, logHidden: false, retryHidden: false, loginHidden: true, status: /安装中.*downloading/, retry: true, locked:true, historyHidden:false}
  );
  await runCase(
    {request_id: installId, script_id: 'demo'},
    {ok: true, status: 200, body: JSON.stringify({ok:true,install_id:installId,running: false, status: '结果未知', phase: 'unknown'})},
    {calls: 1, logHidden: false, retryHidden: false, loginHidden: true, status: /结果未知/, locked:true, historyHidden:false}
  );
  await runCase(
    {install_id: installId, script_id: 'demo'},
    {ok: false, status: 502, body: JSON.stringify({msg: '上游暂不可用'})},
    {calls: 1, logHidden: false, retryHidden: false, loginHidden: true, status: /上游暂不可用/, locked:true, historyHidden:false}
  );
  await runCase(
    {install_id: installId, script_id: 'demo'},
    {ok: true, status: 200, body: '<html>gateway timeout</html>'},
    {calls: 1, logHidden: false, retryHidden: false, loginHidden: true, status: /响应解析失败/, locked:true, historyHidden:false}
  );
  await runCase(
    {install_id: installId, script_id: 'demo'},
    {ok: false, status: 403, body: JSON.stringify({msg: '登录已过期'})},
    {calls: 1, logHidden: false, retryHidden: false, loginHidden: false, status: /登录已过期/, locked:true, historyHidden:false}
  );
  await runCase(
    {install_id: installId, script_id: 'demo'},
    {throw: 'network disconnected'},
    {calls: 1, logHidden: false, retryHidden: false, loginHidden: true, status: /network disconnected/, locked:true, historyHidden:false}
  );
  await runCase(
    '{not-json',
    {ok: true, status: 200, body: JSON.stringify({running: true})},
    {calls: 0, logHidden: true, retryHidden: true, loginHidden: true, status: /损坏/, locked:true, historyHidden:false}
  );
  await runCase(
    {install_id: '../escape', script_id: 'demo'},
    {ok: true, status: 200, body: JSON.stringify({running: true})},
    {calls: 0, logHidden: true, retryHidden: true, loginHidden: true, status: /损坏/, locked:true, historyHidden:false}
  );
})().catch(error => { console.error(error); process.exitCode = 1; });
"""],
                input=json.dumps({"script": script, "reader": reader}),
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise corrupt install recovery")
    def test_install_selection_corrupt_state_requires_complete_history_and_storage_readback(self):
        with isolated_app() as (app, base_dir):
            self.prepare(base_dir)
            html = app.test_client().get(
                "/online-scripts/install-select/demo", headers={"X-Token": TOKEN},
            ).get_data(as_text=True)
            script = next(
                script for script in re.findall(r"<script>(.*?)</script>", html, re.S)
                if "flsInstallCheckHistory" in script
            )
            source = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
            reader = source[source.index("async function flsReadJsonResponse("):source.index("async function flsReadTextResponse(")]
            result = subprocess.run(["node", "-e", r"""
const vm=require('node:vm'),assert=require('node:assert/strict');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
function setup(raw, response, writeFails=false, readMismatch=false){
  const names=['onlineInstallApiError','onlineInstallApiLogin','onlineInstallRecoveryStatus',
    'onlineInstallRecoveryLog','onlineInstallRecoveryStatusLink','onlineInstallRecoveryHistory',
    'onlineInstallRecoveryConfirm','onlineInstallRecoveryReload','onlineInstallRecoveryRetry',
    'onlineInstallRecoveryResume','onlineInstallRecoveryInstallHistory','onlineInstallRecoveryCheckHistory',
    'onlineInstallRecoveryHistoryItems','onlineInstallRecoveryClear'];
  const elements=Object.fromEntries(names.map(id=>[id,{hidden:true,disabled:false,textContent:'',href:'',onclick:null,
    replaceChildren(){},appendChild(){}}]));
  const calls=[],button={disabled:false};let reloads=0;
  const request={value:'a'.repeat(32)},excluded={value:''};
  const form={addEventListener(){},querySelector:s=>s.includes('request_id')?request:null,
    querySelectorAll:()=>[button],getAttribute:()=>'/online-scripts/install/demo'};
  const storage={value:raw,getItem(){return readMismatch&&this.value!==null?null:this.value;},setItem(_key,value){if(writeFails)throw new Error('storage quota');this.value=value;},removeItem(){this.value=null;}};
  const context={window:{FLS_INSTALL_SCRIPT_ID:'demo',location:{pathname:'/online-scripts/install-select/demo',search:'',reload(){reloads++;}}},
    location:{pathname:'/online-scripts/install-select/demo',search:''},document:{
      getElementById:id=>id==='onlineInstallSelectForm'?form:id==='excludedTaskIndexes'?excluded:elements[id],
      querySelectorAll:()=>[],querySelector:()=>null,
      createElement:()=>({textContent:'',href:'',appendChild(){}})},sessionStorage:storage,
    FormData:class{constructor(){this.values={};}delete(){}set(){}},
    flsGetCsrfToken:()=> 'csrf',flsApplyExcludedToVisible(){},encodeURIComponent,
    confirm:()=>true,fetch:async(url,options)=>{calls.push({url,options});return Array.isArray(response)?response.shift():response;},setTimeout,
    flsResponseErrorDiagnostic:(response,body)=>{let payload=null;try{payload=JSON.parse(body)}catch(_){};return {payload,message:payload&&payload.msg||''};}};
  vm.createContext(context);vm.runInContext(input.reader+input.script,context);
  return {context,elements,form,request,storage,calls,button,reloads:()=>reloads};
}
const item={id:'b'.repeat(32),script_id:'demo',status:'已完成',running:false,scheduler_reload_pending:false};
const snapshot='f'.repeat(64);
const historyResponse=(items,total,offset,has_more,revision=snapshot)=>({ok:true,status:200,
  text:async()=>JSON.stringify({ok:true,items,total,offset,limit:100,has_more,snapshot:revision})});
const okHistory=historyResponse([item],1,0,false);
(async()=>{
  const storageFailure=setup(null,okHistory,true);
  await storageFailure.context.flsInstallSubmitApi('/online-scripts/install/demo',storageFailure.form,storageFailure.button);
  assert.equal(storageFailure.calls.length,0);assert.equal(storageFailure.storage.value,null);
  assert.match(storageFailure.elements.onlineInstallApiError.textContent,/storage quota/);

  const readbackFailure=setup(null,okHistory,false,true);
  await readbackFailure.context.flsInstallSubmitApi('/online-scripts/install/demo',readbackFailure.form,readbackFailure.button);
  assert.equal(readbackFailure.calls.length,0);assert.ok(readbackFailure.storage.value);
  assert.match(readbackFailure.elements.onlineInstallApiError.textContent,/读回校验失败/);

  const corrupt='{"broken":';
  const safe=setup(corrupt,okHistory);
  assert.equal(safe.calls.length,0);assert.equal(safe.form.__flsInstallRecoveryLocked,true);
  assert.equal(safe.elements.onlineInstallRecoveryCheckHistory.hidden,false);
  await safe.elements.onlineInstallRecoveryCheckHistory.onclick();
  assert.equal(safe.calls[0].url,'/api/online-scripts/install-history?limit=100');
  assert.equal(safe.elements.onlineInstallRecoveryClear.hidden,false);
  await safe.elements.onlineInstallRecoveryClear.onclick();
  assert.equal(safe.storage.value,null);assert.equal(safe.reloads(),1);

  const firstPage=Array.from({length:100},(_,index)=>({...item,id:'history-'+index}));
  const lastPage={...item,id:'history-last'};
  const paged=setup(corrupt,[historyResponse(firstPage,101,0,true),historyResponse([lastPage],101,100,false)]);
  await paged.elements.onlineInstallRecoveryCheckHistory.onclick();
  assert.equal(paged.calls.length,2);
  assert.match(paged.calls[1].url,/offset=100/);
  assert.match(paged.calls[1].url,new RegExp('snapshot='+snapshot));
  assert.equal(paged.elements.onlineInstallRecoveryClear.hidden,false);
  assert.match(paged.elements.onlineInstallRecoveryStatus.textContent,/完整安装历史均为已知终态/);

  const changedSnapshot=setup(corrupt,[historyResponse(firstPage,101,0,true),
    historyResponse([lastPage],101,100,false,'e'.repeat(64))]);
  await changedSnapshot.elements.onlineInstallRecoveryCheckHistory.onclick();
  assert.equal(changedSnapshot.elements.onlineInstallRecoveryClear.hidden,true);
  assert.equal(changedSnapshot.storage.value,corrupt);
  assert.match(changedSnapshot.elements.onlineInstallRecoveryStatus.textContent,/未能核实/);

  const repeatedId=setup(corrupt,[historyResponse(firstPage,101,0,true),
    historyResponse([{...item,id:'history-99'}],101,100,false)]);
  await repeatedId.elements.onlineInstallRecoveryCheckHistory.onclick();
  assert.equal(repeatedId.elements.onlineInstallRecoveryClear.hidden,true);
  assert.equal(repeatedId.storage.value,corrupt);

  const active={...item,status:'运行中',running:true};
  const activeCase=setup(corrupt,{ok:true,status:200,text:async()=>JSON.stringify({ok:true,items:[active],total:1,offset:0,limit:100,has_more:false})});
  await activeCase.elements.onlineInstallRecoveryCheckHistory.onclick();
  assert.equal(activeCase.elements.onlineInstallRecoveryClear.hidden,true);assert.equal(activeCase.storage.value,corrupt);

  const partial=setup(corrupt,{ok:true,status:200,text:async()=>JSON.stringify({ok:true,items:[item],total:2,offset:0,limit:100,has_more:true})});
  await partial.elements.onlineInstallRecoveryCheckHistory.onclick();
  assert.equal(partial.elements.onlineInstallRecoveryClear.hidden,true);assert.equal(partial.storage.value,corrupt);

  const failed=setup(corrupt,{ok:false,status:503,text:async()=>'<html>unavailable</html>'});
  await failed.elements.onlineInstallRecoveryCheckHistory.onclick();
  assert.equal(failed.elements.onlineInstallRecoveryClear.hidden,true);assert.equal(failed.storage.value,corrupt);

  const changed=setup(corrupt,okHistory);
  await changed.elements.onlineInstallRecoveryCheckHistory.onclick();
  changed.storage.value='changed-after-check';
  await changed.elements.onlineInstallRecoveryClear.onclick();
  assert.equal(changed.storage.value,'changed-after-check');assert.equal(changed.reloads(),0);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""],input=json.dumps({"script":script,"reader":reader}),capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise import recovery")
    def test_install_selection_refresh_recovers_original_import_request_without_reimport(self):
        with isolated_app() as (app, base_dir):
            self.prepare(base_dir)
            html = app.test_client().get(
                "/online-scripts/install-select/demo", headers={"X-Token": TOKEN},
            ).get_data(as_text=True)
            script = next(
                script for script in re.findall(r"<script>(.*?)</script>", html, re.S)
                if "flsInstallRecoverPending" in script
            )
            source = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
            reader = source[source.index("async function flsReadJsonResponse("):source.index("async function flsReadTextResponse(")]
            result = subprocess.run(
                ["node", "-e", r"""
const vm=require('node:vm'); const assert=require('node:assert/strict');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8')); const id='b'.repeat(32);
const elements=Object.fromEntries(['onlineInstallRecoveryStatus','onlineInstallRecoveryLog','onlineInstallRecoveryStatusLink','onlineInstallRecoveryReload','onlineInstallRecoveryRetry','onlineInstallApiLogin'].map(name=>[name,{hidden:true,disabled:false,textContent:'',href:'',onclick:null}]));
const storage={value:JSON.stringify({request_id:id,script_id:'demo',action:'/online-scripts/import-tasks/demo'}),getItem(){return this.value;},setItem(_k,v){this.value=v;},removeItem(){this.value=null;}};
const form={addEventListener(){},querySelector(){return null;},querySelectorAll(){return[];}}; let calls=0;
const responses=[{ok:true,status:200,body:JSON.stringify({ok:true,request_id:id,state:'scheduler_pending',message:'任务已保存，调度器待恢复'})},{ok:true,status:200,body:JSON.stringify({ok:true,request_id:id,state:'completed',message:'任务已保存，调度器已恢复'})}];
const context={window:{FLS_INSTALL_SCRIPT_ID:'demo',location:{pathname:'/online-scripts/install-select/demo',search:''}},location:{pathname:'/online-scripts/install-select/demo',search:''},document:{getElementById:name=>name==='onlineInstallSelectForm'?form:elements[name],querySelectorAll(){return[];},querySelector(){return null;}},sessionStorage:storage,flsGetCsrfToken:()=> 'csrf',flsApplyExcludedToVisible(){},setTimeout,encodeURIComponent,fetch:async(url,options)=>{calls++;assert.equal(url,(calls===1||calls===3)?'/api/online-scripts/import-tasks-status/'+id:'/api/online-scripts/import-tasks-reload/'+id);assert.equal(options.credentials,'same-origin');const value=calls===1?responses[0]:responses[1]; return {...value,text:async()=>value.body};},flsResponseErrorDiagnostic:()=>({payload:null,message:''})};
context.flsReadJsonResponse=async response=>JSON.parse(await response.text());
vm.createContext(context); vm.runInContext(input.reader+input.script,context);
(async()=>{await new Promise(resolve=>setImmediate(resolve)); assert.equal(calls,1); assert.equal(elements.onlineInstallRecoveryStatusLink.hidden,false); assert.equal(elements.onlineInstallRecoveryStatusLink.href,'/online-scripts/import-tasks-status/'+id); assert.equal(elements.onlineInstallRecoveryReload.hidden,false); await elements.onlineInstallRecoveryReload.onclick(); await new Promise(resolve=>setImmediate(resolve)); assert.equal(calls,3); assert.equal(storage.value,null);})().catch(error=>{console.error(error);process.exitCode=1;});
"""], input=json.dumps({"script": script, "reader": reader}), capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise original import retry")
    def test_install_selection_retries_failed_import_with_original_identity_and_selection(self):
        with isolated_app() as (app, base_dir):
            self.prepare(base_dir)
            html = app.test_client().get(
                "/online-scripts/install-select/demo", headers={"X-Token": TOKEN},
            ).get_data(as_text=True)
            script = next(
                script for script in re.findall(r"<script>(.*?)</script>", html, re.S)
                if "flsInstallSubmitApi" in script
            )
            source = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
            reader = source[source.index("async function flsReadJsonResponse("):source.index("async function flsReadTextResponse(")]
            result = subprocess.run(["node", "-e", r"""
const vm=require('node:vm'),assert=require('node:assert/strict');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const id='d'.repeat(32),excluded={value:'2'},enable={checked:true},importTask={checked:true},proxy={value:'proxy-a'};
const elements=Object.fromEntries(['onlineInstallApiError','onlineInstallApiLogin','onlineInstallRecoveryStatus',
  'onlineInstallRecoveryLog','onlineInstallRecoveryStatusLink','onlineInstallRecoveryHistory',
  'onlineInstallRecoveryConfirm','onlineInstallRecoveryReload','onlineInstallRecoveryRetry',
  'onlineInstallRecoveryResume','onlineInstallRecoveryInstallHistory','onlineInstallRecoveryCheckHistory',
  'onlineInstallRecoveryHistoryItems','onlineInstallRecoveryClear'].map(name=>[name,{hidden:true,disabled:false,textContent:'',href:'',onclick:null,replaceChildren(){},appendChild(){}}]));
const stored={request_id:id,script_id:'demo',action:'/online-scripts/import-tasks/demo',
  excluded_task_indexes:'2',enable_task:true,import_task:true,proxy_id:'proxy-a'};
const storage={value:JSON.stringify(stored),getItem(){return this.value;},setItem(_k,v){this.value=v;},removeItem(){this.value=null;}};
const request={value:'new-generated-id'},submitter={disabled:false,getAttribute:()=>'/online-scripts/import-tasks/demo'};
const form={__flsInstallRecoveryLocked:false,addEventListener(){},getAttribute:()=>'/online-scripts/install/demo',
  querySelector:s=>s.includes('request_id')?request:s.includes('enable_task')?enable:s.includes('import_task')?importTask:s.includes('proxy_id')?proxy:null,
  querySelectorAll:()=>[submitter]};
const calls=[];const responses=[
 {ok:true,status:200,body:JSON.stringify({ok:true,request_id:id,state:'failed',retry_allowed:true,message:'未写入任务'})},
 {ok:true,status:200,body:JSON.stringify({ok:true,request_id:id,state:'running',retry_allowed:false,message:'导入已继续'})}
];
class FormDataMock{constructor(){this.values={request_id:request.value,select_mode:'all',excluded_task_indexes:excluded.value,
  enable_task:enable.checked?'1':'0',import_task:importTask.checked?'1':'0',proxy_id:proxy.value};}delete(k){delete this.values[k];}set(k,v){this.values[k]=v;}}
const context={window:{FLS_INSTALL_SCRIPT_ID:'demo',location:{pathname:'/online-scripts/install-select/demo',search:''}},
 location:{pathname:'/online-scripts/install-select/demo',search:''},document:{
  getElementById:name=>name==='onlineInstallSelectForm'?form:name==='excludedTaskIndexes'?excluded:elements[name],
  querySelectorAll:()=>[],querySelector:()=>null,createElement:()=>({textContent:'',href:'',appendChild(){}})},
 sessionStorage:storage,FormData:FormDataMock,flsGetCsrfToken:()=> 'csrf-token',flsApplyExcludedToVisible(){},
 encodeURIComponent,confirm:()=>true,setTimeout,fetch:async(url,options)=>{
  calls.push({url,options});const value=responses[calls.length-1];return {ok:value.ok,status:value.status,text:async()=>value.body};},
 flsResponseErrorDiagnostic:()=>({payload:null,message:''})};
vm.createContext(context);vm.runInContext(input.reader+input.script,context);
(async()=>{
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(calls.length,1);assert.equal(calls[0].url,'/api/online-scripts/import-tasks-status/'+id);
 assert.equal(request.value,id);assert.equal(excluded.value,'2');assert.equal(enable.checked,true);assert.equal(importTask.checked,true);assert.equal(proxy.value,'proxy-a');
 assert.equal(elements.onlineInstallRecoveryResume.hidden,false);
 await elements.onlineInstallRecoveryResume.onclick();
 assert.equal(calls.length,2);assert.equal(calls[1].url,'/api/online-scripts/import-tasks/demo');
 assert.equal(calls[1].options.method,'POST');assert.equal(calls[1].options.headers['X-CSRF-Token'],'csrf-token');
 assert.equal(calls[1].options.body.values.request_id,id);assert.equal(calls[1].options.body.values.excluded_task_indexes,'2');
 assert.equal(calls[1].options.body.values.enable_task,'1');assert.equal(calls[1].options.body.values.proxy_id,'proxy-a');
 assert.equal(storage.value,JSON.stringify(stored));assert.equal(context.window.location.href,'/online-scripts/import-tasks-status/'+id);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""],input=json.dumps({"script":script,"reader":reader}),capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

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
                          if "async function loadLog(" in script)
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
      document: {documentElement: {scrollHeight: 800}, getElementById: id => elements[id], querySelector: () => null},
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
      document: {documentElement: {scrollHeight: 800}, getElementById: id => elements[id], querySelector: () => null},
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
