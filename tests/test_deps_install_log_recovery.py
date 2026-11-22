import json
import re
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests.test_ui_route_components import ROOT, TOKEN, isolated_app


class DependencyInstallLogRecoveryTests(unittest.TestCase):
    def seed_running_install(self, base_dir, install_id="cancel-install", kind="dependency", process=None):
        from fls_manager.install_records import persist_install_job
        from fls_manager.state import DEPS_RUNNING

        package = "demo" if kind == "dependency" else "node"
        prefix = "deps-install-demo" if kind == "dependency" else "system-install-node"
        log_file = base_dir / "log" / f"{prefix}-{install_id}.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fp = log_file.open("ab", buffering=0)
        process = process or SimpleNamespace(poll=lambda: None)
        info = {
            "id": install_id, "request_id": install_id, "kind": kind, "package": package,
            "log_file": str(log_file), "running": True, "finished": False,
            "status": "running", "returncode": None, "error": "", "start_time": 1,
            "updated_at": "2026-11-18 19:00:00", "process": process, "log_fp": fp,
        }
        persist_install_job(info)
        DEPS_RUNNING[install_id] = info
        return info, fp

    def test_install_request_replay_is_idempotent_for_dependency_and_runtime(self):
        for runtime in (False, True):
            with self.subTest(runtime=runtime), isolated_app() as (app, base_dir):
                from fls_manager.state import DEPS_RUNNING

                proc = SimpleNamespace(returncode=None)
                proc.poll = lambda: proc.returncode
                endpoint = "/install/runtime/node" if runtime else "/deps/install"
                data = {"request_id": "replay-install-1", "name": "demo-package"}
                if runtime:
                    data = {"request_id": "replay-runtime-1"}
                module = "runtime" if runtime else "deps"
                with patch(
                    f"fls_manager.routes.{module}.subprocess.Popen", return_value=proc,
                ) as popen, patch(
                    "fls_manager.routes.runtime.runtime_install_command",
                    return_value="echo runtime",
                ):
                    first = app.test_client().post(endpoint, data=data, headers={"X-Token": TOKEN})
                    second = app.test_client().post(endpoint, data=data, headers={"X-Token": TOKEN})
                self.assertEqual(first.status_code, 302)
                self.assertEqual(second.status_code, 302)
                self.assertEqual(first.headers["Location"], second.headers["Location"])
                self.assertEqual(popen.call_count, 1)
                install_id = first.headers["Location"].split("/deps/install-log/", 1)[1].split("?", 1)[0]
                self.assertIn(install_id, DEPS_RUNNING)
                self.assertEqual(DEPS_RUNNING[install_id]["request_id"], install_id)
                history = app.test_client().get(
                    "/api/deps/install-history", headers={"X-Token": TOKEN},
                )
                self.assertEqual(history.status_code, 200)
                self.assertEqual(history.json["items"][0]["id"], install_id)
                self.assertEqual(history.json["items"][0]["status"], "running")

    def test_dependency_install_json_api_returns_stable_replay_state(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.state import DEPS_RUNNING

            proc = SimpleNamespace(poll=lambda: None)
            with patch("fls_manager.routes.deps.subprocess.Popen", return_value=proc) as popen:
                first = app.test_client().post(
                    "/api/deps/install",
                    json={"request_id": "api-install-1", "name": "demo-package"},
                    headers={"X-Token": TOKEN},
                )
                replay = app.test_client().post(
                    "/api/deps/install",
                    json={"request_id": "api-install-1", "name": "demo-package"},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(first.status_code, 200)
            self.assertTrue(first.json["ok"])
            self.assertEqual(first.json["request_id"], "api-install-1")
            self.assertEqual(replay.status_code, 200)
            self.assertTrue(replay.json["ok"])
            self.assertEqual(replay.json["request_id"], first.json["request_id"])
            self.assertEqual(popen.call_count, 1)
            self.assertIn("api-install-1", DEPS_RUNNING)

    def test_runtime_install_json_api_replays_without_starting_second_process(self):
        with isolated_app() as (app, _base_dir):
            proc = SimpleNamespace(poll=lambda: None)
            with patch("fls_manager.routes.runtime.runtime_install_command", return_value="echo runtime"), patch(
                "fls_manager.routes.runtime.subprocess.Popen", return_value=proc,
            ) as popen:
                first = app.test_client().post(
                    "/api/install/runtime/node",
                    json={"request_id": "api-runtime-1"},
                    headers={"X-Token": TOKEN},
                )
                replay = app.test_client().post(
                    "/api/install/runtime/node",
                    json={"request_id": "api-runtime-1"},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(first.status_code, 200)
            self.assertEqual(replay.status_code, 200)
            self.assertEqual(replay.json["request_id"], "api-runtime-1")
            self.assertEqual(popen.call_count, 1)

    def test_json_replay_reports_persisted_terminal_state(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.install_records import persist_install_job

            persist_install_job({
                "id": "finished-install", "request_id": "finished-install",
                "kind": "dependency", "package": "demo-package", "log_file": str(_base_dir / "log" / "deps-install-demo-finished-install.log"),
                "running": False, "finished": True, "status": "succeeded",
                "returncode": 0, "error": "", "start_time": 1,
                "updated_at": "2026-11-18 17:00:00",
            })
            persist_install_job({
                "id": "failed-runtime", "request_id": "failed-runtime",
                "kind": "runtime", "package": "node", "log_file": str(_base_dir / "log" / "system-install-node-failed-runtime.log"),
                "running": False, "finished": True, "status": "failed",
                "returncode": 7, "error": "package manager failed", "start_time": 1,
                "updated_at": "2026-11-18 17:01:00",
            })
            client = app.test_client()
            with patch("fls_manager.routes.deps.subprocess.Popen") as dep_popen, patch(
                "fls_manager.routes.runtime.subprocess.Popen"
            ) as runtime_popen:
                succeeded = client.post(
                    "/api/deps/install", json={"request_id": "finished-install", "name": "demo-package"},
                    headers={"X-Token": TOKEN},
                )
                failed = client.post(
                    "/api/install/runtime/node", json={"request_id": "failed-runtime"},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(succeeded.status_code, 200)
            self.assertEqual(succeeded.json["status"], "succeeded")
            self.assertFalse(succeeded.json["running"])
            self.assertEqual(succeeded.json["returncode"], 0)
            self.assertEqual(failed.status_code, 200)
            self.assertEqual(failed.json["status"], "failed")
            self.assertFalse(failed.json["running"])
            self.assertEqual(failed.json["returncode"], 7)
            dep_popen.assert_not_called()
            runtime_popen.assert_not_called()

    def test_failed_install_retry_is_stable_and_links_original(self):
        for runtime in (False, True):
            with self.subTest(runtime=runtime), isolated_app() as (app, base_dir):
                from fls_manager.install_records import persist_install_job
                from fls_manager.state import DEPS_RUNNING

                original = "failed-runtime" if runtime else "failed-dependency"
                package = "node" if runtime else "demo-package"
                log_name = "system-install-node" if runtime else "deps-install-demo-package"
                log_file = base_dir / "log" / f"{log_name}-{original}.log"
                log_file.parent.mkdir(parents=True, exist_ok=True)
                log_file.write_text("previous attempt\n", encoding="utf-8")
                persist_install_job({
                    "id": original, "request_id": original,
                    "kind": "runtime" if runtime else "dependency", "package": package,
                    "log_file": str(log_file), "running": False, "finished": True,
                    "status": "unknown" if runtime else "failed", "returncode": None if runtime else 7,
                    "error": "previous result", "start_time": 1,
                    "updated_at": "2026-11-18 18:00:00",
                })
                proc = SimpleNamespace(returncode=None)
                proc.poll = lambda: proc.returncode
                endpoint = f"/api/deps/install-retry/{original}"
                patchers = [patch("fls_manager.routes.deps.subprocess.Popen", return_value=proc)]
                if runtime:
                    patchers.append(patch("fls_manager.routes.runtime.runtime_install_command", return_value="echo runtime"))
                with patchers[0] as popen:
                    runtime_patch = patchers[1] if runtime else None
                    if runtime_patch:
                        runtime_patch.start()
                    try:
                        client = app.test_client()
                        first = client.post(
                            endpoint, json={"request_id": f"retry-{runtime}"},
                            headers={"X-Token": TOKEN},
                        )
                        proc.returncode = 7
                        replay = client.post(
                            endpoint, json={"request_id": f"retry-{runtime}"},
                            headers={"X-Token": TOKEN},
                        )
                    finally:
                        if runtime_patch:
                            runtime_patch.stop()
                self.assertEqual(first.status_code, 200)
                self.assertTrue(first.json["ok"])
                self.assertEqual(first.json["install_id"], f"retry-{runtime}")
                self.assertEqual(first.json["status"], "running")
                self.assertEqual(replay.status_code, 200)
                self.assertEqual(replay.json["install_id"], first.json["install_id"])
                self.assertEqual(replay.json["status"], "failed")
                self.assertEqual(replay.json["returncode"], 7)
                self.assertEqual(popen.call_count, 1)
                from fls_manager.install_records import get_install_job
                self.assertEqual(get_install_job(original)["retried_by"], f"retry-{runtime}")
                self.assertEqual(DEPS_RUNNING[f"retry-{runtime}"]["retry_of"], original)

    def test_retry_start_failure_is_durable_and_replay_does_not_restart(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.install_records import persist_install_job
            from fls_manager.state import DEPS_RUNNING

            original = "retry-start-failure"
            original_log = base_dir / "log" / f"deps-install-demo-{original}.log"
            original_log.parent.mkdir(parents=True, exist_ok=True)
            persist_install_job({
                "id": original, "request_id": original, "kind": "dependency",
                "package": "demo", "log_file": str(original_log), "running": False,
                "finished": True, "status": "failed", "returncode": 1,
                "error": "failed", "start_time": 1, "updated_at": "2026-11-18 18:01:00",
            })
            with patch("fls_manager.routes.deps.subprocess.Popen", side_effect=OSError("quota")) as popen:
                client = app.test_client()
                first = client.post(
                    f"/api/deps/install-retry/{original}",
                    json={"request_id": "retry-start-child"}, headers={"X-Token": TOKEN},
                )
                replay = client.post(
                    f"/api/deps/install-retry/{original}",
                    json={"request_id": "retry-start-child"}, headers={"X-Token": TOKEN},
                )
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.json["status"], "start_failed")
            self.assertIn("quota", first.json["error"])
            self.assertEqual(replay.status_code, 200)
            self.assertEqual(replay.json["install_id"], "retry-start-child")
            self.assertEqual(replay.json["status"], "start_failed")
            self.assertEqual(popen.call_count, 1)
            from fls_manager.install_records import get_install_job
            self.assertEqual(get_install_job(original)["retried_by"], "retry-start-child")

    def test_retry_rejects_success_or_conflicting_request_without_starting(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.install_records import persist_install_job

            for status, code in (("succeeded", 0), ("running", None)):
                install_id = f"cannot-retry-{status}"
                persist_install_job({
                    "id": install_id, "request_id": install_id, "kind": "dependency",
                    "package": "demo", "log_file": str(base_dir / "log" / f"{install_id}.log"),
                    "running": status == "running", "finished": status != "running",
                    "status": status, "returncode": code, "error": "", "start_time": 1,
                    "updated_at": "2026-11-18 18:02:00",
                })
                if status == "running":
                    from fls_manager.state import DEPS_RUNNING
                    DEPS_RUNNING[install_id] = {
                        "id": install_id, "request_id": install_id, "kind": "dependency",
                        "package": "demo", "log_file": str(base_dir / "log" / f"{install_id}.log"),
                        "running": True, "finished": False, "status": "running",
                        "process": SimpleNamespace(poll=lambda: None), "log_fp": None,
                        "returncode": None, "error": "", "updated_at": "2026-11-18 18:02:00",
                    }
                with patch(
                    "fls_manager.routes.deps.subprocess.Popen",
                    return_value=SimpleNamespace(poll=lambda: None),
                ) as popen:
                    response = app.test_client().post(
                        f"/api/deps/install-retry/{install_id}",
                        json={"request_id": f"new-{status}"}, headers={"X-Token": TOKEN},
                    )
                self.assertEqual(response.status_code, 409)
                self.assertFalse(response.json["ok"])
                popen.assert_not_called()

            original = "conflict-original"
            persist_install_job({
                "id": original, "request_id": original, "kind": "dependency", "package": "demo",
                "log_file": str(base_dir / "log" / "conflict.log"), "running": False,
                "finished": True, "status": "failed", "returncode": 1, "error": "failed",
                "start_time": 1, "updated_at": "2026-11-18 18:03:00",
            })
            with patch(
                "fls_manager.routes.deps.subprocess.Popen",
                return_value=SimpleNamespace(poll=lambda: None),
            ) as popen:
                first = app.test_client().post(
                    f"/api/deps/install-retry/{original}",
                    json={"request_id": "child-a"}, headers={"X-Token": TOKEN},
                )
                second = app.test_client().post(
                    f"/api/deps/install-retry/{original}",
                    json={"request_id": "child-b"}, headers={"X-Token": TOKEN},
                )
            self.assertEqual(first.status_code, 200)
            self.assertEqual(second.status_code, 409)
            self.assertEqual(second.json["install_id"], "child-a")
            popen.assert_called_once()

            status = app.test_client().get(
                f"/api/deps/install-retry/{original}/child-a", headers={"X-Token": TOKEN},
            )
            self.assertEqual(status.status_code, 200)
            self.assertEqual(status.json["install_id"], "child-a")
            self.assertFalse(status.json["retry_available"])

    def test_retry_status_does_not_mark_a_live_parent_unknown(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.install_records import INSTALL_JOBS_FILE, get_install_job, persist_install_job
            from fls_manager.state import DEPS_RUNNING

            install_id = "live-retry-parent"
            persist_install_job({
                "id": install_id, "request_id": install_id, "kind": "dependency",
                "package": "demo", "log_file": str(base_dir / "log" / "live.log"),
                "running": True, "finished": False, "status": "running", "returncode": None,
                "error": "", "start_time": 1, "updated_at": "2026-11-18 18:03:30",
            })
            DEPS_RUNNING[install_id] = {
                "id": install_id, "request_id": install_id, "kind": "dependency",
                "package": "demo", "log_file": str(base_dir / "log" / "live.log"),
                "running": True, "finished": False, "status": "running",
                "process": SimpleNamespace(poll=lambda: None), "log_fp": None,
                "returncode": None, "error": "", "updated_at": "2026-11-18 18:03:30",
            }
            response = app.test_client().get(
                f"/api/deps/install-retry/{install_id}/not-started-child", headers={"X-Token": TOKEN},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["status"], "original_running")
            self.assertFalse(response.json["retry_available"])
            persisted = json.loads(INSTALL_JOBS_FILE.read_text(encoding="utf-8"))
            self.assertEqual(persisted[install_id]["status"], "running")

    def test_retry_form_requires_csrf_and_renders_recovery_controls(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.install_records import persist_install_job

            install_id = "form-retry"
            log_file = base_dir / "log" / f"deps-install-demo-{install_id}.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text("failed\n", encoding="utf-8")
            persist_install_job({
                "id": install_id, "request_id": install_id, "kind": "dependency",
                "package": "demo", "log_file": str(log_file), "running": False,
                "finished": True, "status": "failed", "returncode": 1, "error": "failed",
                "start_time": 1, "updated_at": "2026-11-18 18:04:00",
            })
            client = app.test_client()
            denied = client.post(
                f"/deps/install-retry/{install_id}", data={"request_id": "form-child"},
            )
            self.assertEqual(denied.status_code, 400)
            page = client.get(f"/deps/install-log/{install_id}", headers={"X-Token": TOKEN})
            body = page.get_data(as_text=True)
            self.assertIn(f'action="/deps/install-retry/{install_id}"', body)
            self.assertIn(f'data-api-action="/api/deps/install-retry/{install_id}"', body)
            self.assertIn('id="installRetryError"', body)
            self.assertIn('id="installRetryLogin"', body)

    def test_no_javascript_retry_form_uses_same_idempotent_transaction(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.install_records import get_install_job, persist_install_job

            install_id = "html-retry-parent"
            log_file = base_dir / "log" / f"deps-install-demo-{install_id}.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text("failed\n", encoding="utf-8")
            persist_install_job({
                "id": install_id, "request_id": install_id, "kind": "dependency",
                "package": "demo", "log_file": str(log_file), "running": False,
                "finished": True, "status": "failed", "returncode": 1, "error": "failed",
                "start_time": 1, "updated_at": "2026-11-18 18:04:30",
            })
            proc = SimpleNamespace(poll=lambda: None)
            with patch("fls_manager.routes.deps.subprocess.Popen", return_value=proc) as popen:
                response = app.test_client().post(
                    f"/deps/install-retry/{install_id}",
                    data={"request_id": "html-retry-child"}, headers={"X-Token": TOKEN},
                )
            self.assertEqual(response.status_code, 302)
            self.assertIn("/deps/install-log/html-retry-child", response.headers["Location"])
            self.assertEqual(get_install_job(install_id)["retried_by"], "html-retry-child")
            popen.assert_called_once()

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise retry recovery")
    def test_retry_page_queries_saved_request_after_reload_and_recovers_errors(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.install_records import persist_install_job

            install_id = "retry-browser-parent"
            log_file = base_dir / "log" / f"deps-install-demo-{install_id}.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text("failed\n", encoding="utf-8")
            persist_install_job({
                "id": install_id, "request_id": install_id, "kind": "dependency",
                "package": "demo", "log_file": str(log_file), "running": False,
                "finished": True, "status": "failed", "returncode": 1, "error": "failed",
                "start_time": 1, "updated_at": "2026-11-18 18:05:00",
            })
            html = app.test_client().get(
                f"/deps/install-log/{install_id}", headers={"X-Token": TOKEN},
            ).get_data(as_text=True)
            script = next(item for item in re.findall(r"<script>(.*?)</script>", html, re.S) if "function showRetryFailure" in item)
            harness = r"""
const vm=require('node:vm'),assert=require('node:assert/strict');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const listeners={},calls=[],stored={"fls-deps-install-retry:retry-browser-parent":JSON.stringify({request_id:'stable-child',original_id:'retry-browser-parent'})};
const elements=Object.fromEntries(['installRetryError','installRetryQuery','installRetryLog','installRetryLogin'].map(id=>[id,{hidden:true,textContent:'',href:'',disabled:false,addEventListener(name,fn){listeners[id+':'+name]=fn;}}]));
const requestId={value:'new-child'},button={disabled:false},form={dataset:{apiAction:'/api/deps/install-retry/retry-browser-parent'},querySelector(selector){return selector.includes('request_id')?requestId:button;},addEventListener(name,fn){listeners['form:'+name]=fn;}};
let response={ok:true,status:200},payload={ok:true,status:'running',status_label:'安装中',running:true,install_id:'stable-child',request_id:'stable-child',log_url:'/deps/install-log/stable-child'};
const context={document:{querySelector:()=>form,getElementById:id=>elements[id]},sessionStorage:{getItem:key=>stored[key]||null,setItem:(key,value)=>{stored[key]=value;}},location:{pathname:'/deps/install-log/retry-browser-parent',search:''},fetch:async(url,options)=>{calls.push([url,options]);return response;},flsReadJsonResponse:async()=>payload,flsGetCsrfToken:()=> 'csrf-token',encodeURIComponent};
vm.createContext(context);vm.runInContext(input.script,context);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 await tick();
 assert.equal(calls[0][0],'/api/deps/install-retry/retry-browser-parent/stable-child');
 assert.equal(elements.installRetryLog.hidden,false);
 assert.equal(elements.installRetryLog.href,'/deps/install-log/stable-child');
 assert.equal(elements.installRetryError.textContent,'原重试状态：安装中');
 response={ok:false,status:502};payload={ok:false,msg:'upstream unavailable'};
 await listeners['installRetryQuery:click']();
 assert.equal(requestId.value,'stable-child');
 assert.equal(elements.installRetryQuery.hidden,false);
 assert.match(elements.installRetryError.textContent,/upstream unavailable/);
 response={ok:false,status:401};payload={ok:false,msg:'登录已过期'};
 await listeners['installRetryQuery:click']();
 assert.equal(elements.installRetryLogin.hidden,false);
 assert.match(elements.installRetryLogin.href,/^\/login\?next=/);
 assert.equal(requestId.value,'stable-child');
 console.log('retry request recovery passed');
})().catch(error=>{console.error(error);process.exitCode=1});
"""
            result = subprocess.run(
                ["node", "-e", harness], input=json.dumps({"script": script}),
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_concurrent_retry_claim_starts_only_one_process(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.install_records import persist_install_job

            original = "concurrent-retry-parent"
            original_log = base_dir / "log" / f"deps-install-demo-{original}.log"
            original_log.parent.mkdir(parents=True, exist_ok=True)
            persist_install_job({
                "id": original, "request_id": original, "kind": "dependency",
                "package": "demo", "log_file": str(original_log), "running": False,
                "finished": True, "status": "failed", "returncode": 1, "error": "failed",
                "start_time": 1, "updated_at": "2026-11-18 18:06:00",
            })
            entered, release = threading.Event(), threading.Event()

            def start_process(*_args, **_kwargs):
                entered.set()
                self.assertTrue(release.wait(5))
                return SimpleNamespace(poll=lambda: None)

            with patch("fls_manager.routes.deps.subprocess.Popen", side_effect=start_process) as popen:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    first_future = pool.submit(
                        lambda: app.test_client().post(
                            f"/api/deps/install-retry/{original}", json={"request_id": "same-child"},
                            headers={"X-Token": TOKEN},
                        )
                    )
                    self.assertTrue(entered.wait(5))
                    replay = app.test_client().post(
                        f"/api/deps/install-retry/{original}", json={"request_id": "same-child"},
                        headers={"X-Token": TOKEN},
                    )
                    release.set()
                    first = first_future.result(timeout=5)
            self.assertEqual(first.status_code, 200)
            self.assertEqual(replay.status_code, 200)
            self.assertEqual(first.json["install_id"], "same-child")
            self.assertEqual(replay.json["install_id"], "same-child")
            popen.assert_called_once()

    def test_cancel_persists_intent_and_confirmed_exit_then_replays_without_signal(self):
        for kind in ("dependency", "runtime"):
            with self.subTest(kind=kind), isolated_app() as (app, base_dir):
                from fls_manager.install_records import get_install_job

                process = SimpleNamespace(
                    returncode=-15, poll=lambda: None,
                    terminate=unittest.mock.Mock(), kill=unittest.mock.Mock(),
                    wait=unittest.mock.Mock(return_value=-15),
                )
                info, fp = self.seed_running_install(base_dir, f"cancel-{kind}", kind, process)
                client = app.test_client()
                first = client.post(
                    f"/api/deps/install-cancel/{info['id']}",
                    json={"request_id": "stable-cancel-id"}, headers={"X-Token": TOKEN},
                )
                replay = client.post(
                    f"/api/deps/install-cancel/{info['id']}",
                    json={"request_id": "stable-cancel-id"}, headers={"X-Token": TOKEN},
                )
                self.assertEqual(first.status_code, 200)
                self.assertEqual(first.json["status"], "cancelled")
                self.assertEqual(first.json["cancel_status"], "cancelled")
                self.assertFalse(first.json["running"])
                self.assertEqual(replay.status_code, 200)
                self.assertEqual(replay.json["cancel_status"], "cancelled")
                process.terminate.assert_called_once()
                process.wait.assert_called_once_with(timeout=3)
                process.kill.assert_not_called()
                self.assertTrue(fp.closed)
                record = get_install_job(info["id"])
                self.assertEqual(record["status"], "cancelled")
                self.assertEqual(record["cancel_request_id"], "stable-cancel-id")

    def test_cancel_timeout_kills_process_and_reports_cancelled(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.state import DEPS_RUNNING

            process = SimpleNamespace(
                poll=lambda: None,
                terminate=unittest.mock.Mock(),
                kill=unittest.mock.Mock(),
                wait=unittest.mock.Mock(side_effect=[subprocess.TimeoutExpired("install", 3), -9]),
            )
            info, _fp = self.seed_running_install(base_dir, process=process)
            response = app.test_client().post(
                f"/api/deps/install-cancel/{info['id']}",
                json={"request_id": "cancel-timeout"}, headers={"X-Token": TOKEN},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["status"], "cancelled")
            process.terminate.assert_called_once()
            process.kill.assert_called_once()
            self.assertEqual(process.wait.call_count, 2)
            self.assertTrue(DEPS_RUNNING[info["id"]]["finished"])

    def test_cancel_kill_failure_keeps_running_state_and_allows_a_new_attempt(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.install_records import get_install_job
            from fls_manager.state import DEPS_RUNNING

            waits = [
                subprocess.TimeoutExpired("install", 3),
                subprocess.TimeoutExpired("install", 3),
                -9,
            ]
            kills = [OSError("kill denied"), None]
            process = SimpleNamespace(
                poll=lambda: None,
                terminate=unittest.mock.Mock(),
                kill=unittest.mock.Mock(side_effect=kills),
                wait=unittest.mock.Mock(side_effect=waits),
            )
            info, _fp = self.seed_running_install(base_dir, process=process)
            client = app.test_client()
            response = client.post(
                f"/api/deps/install-cancel/{info['id']}",
                json={"request_id": "cancel-kill-failed"}, headers={"X-Token": TOKEN},
            )
            self.assertEqual(response.status_code, 503)
            self.assertTrue(response.json["running"])
            self.assertEqual(response.json["status"], "running")
            self.assertEqual(response.json["cancel_status"], "failed")
            record = get_install_job(info["id"], DEPS_RUNNING)
            self.assertTrue(record["running"])
            self.assertEqual(record["cancel_status"], "failed")
            self.assertIn("kill denied", record["cancel_error"])
            queried = client.get(
                f"/api/deps/install-cancel/{info['id']}/cancel-kill-failed",
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(queried.status_code, 200)
            self.assertTrue(queried.json["ok"])
            self.assertEqual(queried.json["cancel_status"], "failed")
            self.assertTrue(queried.json["running"])
            retried = client.post(
                f"/api/deps/install-cancel/{info['id']}",
                json={"request_id": "cancel-kill-retry"}, headers={"X-Token": TOKEN},
            )
            self.assertEqual(retried.status_code, 200)
            self.assertEqual(retried.json["cancel_status"], "cancelled")
            self.assertEqual(process.terminate.call_count, 2)
            self.assertEqual(process.kill.call_count, 2)
            self.assertEqual(process.wait.call_count, 3)

    def test_cancel_race_with_successful_exit_reports_completed_not_cancelled(self):
        with isolated_app() as (app, base_dir):
            process_state = {"returncode": None}

            def terminate():
                process_state["returncode"] = 0

            process = SimpleNamespace(
                poll=lambda: process_state["returncode"],
                terminate=unittest.mock.Mock(side_effect=terminate),
                kill=unittest.mock.Mock(),
                wait=unittest.mock.Mock(return_value=0),
            )
            info, _fp = self.seed_running_install(base_dir, process=process)
            response = app.test_client().post(
                f"/api/deps/install-cancel/{info['id']}",
                json={"request_id": "cancel-raced-success"}, headers={"X-Token": TOKEN},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["status"], "succeeded")
            self.assertEqual(response.json["cancel_status"], "completed")
            process.terminate.assert_called_once()
            process.kill.assert_not_called()

    def test_cancelled_install_can_be_explicitly_retried_once(self):
        with isolated_app() as (app, base_dir):
            cancelled_process = SimpleNamespace(
                poll=lambda: None,
                terminate=unittest.mock.Mock(),
                kill=unittest.mock.Mock(),
                wait=unittest.mock.Mock(return_value=-15),
            )
            info, _fp = self.seed_running_install(base_dir, process=cancelled_process)
            client = app.test_client()
            cancelled = client.post(
                f"/api/deps/install-cancel/{info['id']}",
                json={"request_id": "cancel-before-retry"}, headers={"X-Token": TOKEN},
            )
            self.assertEqual(cancelled.status_code, 200)
            self.assertEqual(cancelled.json["status"], "cancelled")

            retry_process = SimpleNamespace(poll=lambda: None)
            with patch("fls_manager.routes.deps.subprocess.Popen", return_value=retry_process) as popen:
                first = client.post(
                    f"/api/deps/install-retry/{info['id']}",
                    json={"request_id": "cancel-retry-child"}, headers={"X-Token": TOKEN},
                )
                replay = client.post(
                    f"/api/deps/install-retry/{info['id']}",
                    json={"request_id": "cancel-retry-child"}, headers={"X-Token": TOKEN},
                )
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.json["install_id"], "cancel-retry-child")
            self.assertEqual(replay.status_code, 200)
            self.assertEqual(replay.json["install_id"], "cancel-retry-child")
            self.assertEqual(popen.call_count, 1)

    def test_cancel_refuses_missing_csrf_and_completed_install_without_signal(self):
        with isolated_app() as (app, base_dir):
            process = SimpleNamespace(
                poll=unittest.mock.Mock(return_value=0),
                terminate=unittest.mock.Mock(),
                wait=unittest.mock.Mock(),
                kill=unittest.mock.Mock(),
            )
            info, _fp = self.seed_running_install(base_dir, process=process)
            client = app.test_client()
            denied = client.post(
                f"/api/deps/install-cancel/{info['id']}", json={"request_id": "cancel-no-csrf"},
            )
            self.assertEqual(denied.status_code, 400)
            process.terminate.assert_not_called()
            unauthenticated = app.test_client().get(
                f"/api/deps/install-cancel/{info['id']}/cancel-auth-check",
            )
            self.assertEqual(unauthenticated.status_code, 401)
            process.terminate.assert_not_called()
            completed = client.post(
                f"/api/deps/install-cancel/{info['id']}",
                json={"request_id": "cancel-after-finish"}, headers={"X-Token": TOKEN},
            )
            self.assertEqual(completed.status_code, 409)
            self.assertEqual(completed.json["status"], "succeeded")
            process.terminate.assert_not_called()

    def test_cancel_after_restart_is_unknown_and_never_claims_success(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.install_records import INSTALL_JOBS_FILE
            from fls_manager.state import DEPS_RUNNING

            info, fp = self.seed_running_install(base_dir)
            fp.close()
            info.update(cancel_request_id="restart-cancel-id", cancel_status="requested")
            from fls_manager.install_records import persist_install_job
            persist_install_job(info)
            DEPS_RUNNING.clear()
            response = app.test_client().get(
                f"/api/deps/install-cancel/{info['id']}/restart-cancel-id",
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["status"], "unknown")
            self.assertEqual(response.json["cancel_status"], "unknown")
            self.assertNotEqual(response.json["status"], "cancelled")
            persisted = json.loads(INSTALL_JOBS_FILE.read_text(encoding="utf-8"))
            self.assertEqual(persisted[info["id"]]["cancel_status"], "unknown")

    def test_cancel_status_query_polls_a_live_process_before_reporting(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.install_records import persist_install_job

            process = SimpleNamespace(
                poll=unittest.mock.Mock(return_value=0),
                terminate=unittest.mock.Mock(), kill=unittest.mock.Mock(),
                wait=unittest.mock.Mock(),
            )
            info, fp = self.seed_running_install(base_dir, process=process)
            info.update(cancel_request_id="cancel-race-query", cancel_status="requested")
            persist_install_job(info)
            response = app.test_client().get(
                f"/api/deps/install-cancel/{info['id']}/cancel-race-query",
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json["ok"])
            self.assertEqual(response.json["status"], "succeeded")
            self.assertEqual(response.json["cancel_status"], "completed")
            self.assertFalse(response.json["running"])
            process.poll.assert_called_once()
            self.assertTrue(fp.closed)

    def test_running_install_page_has_csrf_cancel_form_and_recovery_controls(self):
        with isolated_app() as (app, base_dir):
            process = SimpleNamespace(
                poll=lambda: None,
                terminate=unittest.mock.Mock(),
                kill=unittest.mock.Mock(),
                wait=unittest.mock.Mock(return_value=-15),
            )
            info, _fp = self.seed_running_install(base_dir, process=process)
            client = app.test_client()
            with client.session_transaction() as session:
                session.update(
                    token=TOKEN, token_expire_at=4102444800,
                    security_verified=True, csrf_token="install-cancel-csrf",
                )
            denied = client.post(
                f"/deps/install-cancel/{info['id']}", data={"request_id": "html-cancel"},
            )
            self.assertEqual(denied.status_code, 400)
            body = client.get(
                f"/deps/install-log/{info['id']}",
            ).get_data(as_text=True)
            self.assertIn(f'action="/deps/install-cancel/{info["id"]}"', body)
            self.assertIn(f'data-api-action="/api/deps/install-cancel/{info["id"]}"', body)
            self.assertIn('id="installCancelQuery"', body)
            self.assertIn('id="installCancelAgain"', body)
            csrf = re.search(r'<meta name="csrf-token" content="([^"]+)"', body).group(1)
            self.assertEqual(csrf, "install-cancel-csrf")
            submitted = client.post(
                f"/deps/install-cancel/{info['id']}",
                data={"request_id": "html-cancel-valid", "csrf_token": csrf},
            )
            replay = client.post(
                f"/deps/install-cancel/{info['id']}",
                data={"request_id": "html-cancel-valid", "csrf_token": csrf},
            )
            self.assertEqual(submitted.status_code, 302)
            self.assertEqual(replay.status_code, 302)
            self.assertEqual(submitted.headers["Location"], replay.headers["Location"])
            process.terminate.assert_called_once()

    def test_refreshed_install_page_queries_persisted_cancel_request_id(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.install_records import persist_install_job

            info, _fp = self.seed_running_install(base_dir)
            info.update(cancel_request_id="stable-cancel-request", cancel_status="requested")
            persist_install_job(info)
            body = app.test_client().get(
                f"/deps/install-log/{info['id']}", headers={"X-Token": TOKEN},
            ).get_data(as_text=True)
            self.assertIn('data-query-on-load="true"', body)
            self.assertIn('name="request_id" value="stable-cancel-request"', body)
            self.assertIn("queryCancel();", body)
            self.assertIn('type="submit" hidden disabled>取消安装</button>', body)

    def test_restart_running_install_becomes_unknown_and_history_is_queryable(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.install_records import INSTALL_JOBS_FILE, persist_install_job
            from fls_manager.state import DEPS_RUNNING

            install_id = "restart-dependency"
            log_file = base_dir / "log" / f"deps-install-demo-{install_id}.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text("install output\n", encoding="utf-8")
            persist_install_job({
                "id": install_id, "request_id": install_id, "kind": "dependency",
                "package": "demo", "log_file": str(log_file), "running": True,
                "status": "running", "returncode": None, "error": "",
                "start_time": 1, "updated_at": "2026-11-18 15:00:00",
            })
            DEPS_RUNNING.clear()
            client = app.test_client()
            self.assertEqual(client.get("/api/deps/install-history").status_code, 401)
            invalid_kind = client.get(
                "/api/deps/install-history?kind=other", headers={"X-Token": TOKEN},
            )
            self.assertEqual(invalid_kind.status_code, 400)
            result = client.get(
                f"/api/deps/install-log/{install_id}", headers={"X-Token": TOKEN},
            )
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json["status"], "unknown")
            self.assertFalse(result.json["running"])
            self.assertIn("install output", result.json["log"])
            history = client.get(
                "/api/deps/install-history?kind=dependency&limit=10",
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(history.status_code, 200)
            self.assertEqual(history.json["items"][0]["id"], install_id)
            self.assertEqual(history.json["items"][0]["status"], "unknown")
            page = client.get("/deps/install-history?kind=dependency", headers={"X-Token": TOKEN})
            self.assertEqual(page.status_code, 200)
            self.assertIn(f"/deps/install-log/{install_id}", page.get_data(as_text=True))
            self.assertTrue(INSTALL_JOBS_FILE.exists())

    def test_install_history_rejects_corrupt_or_symlink_receipt_without_overwrite(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.install_records import INSTALL_JOBS_FILE

            client = app.test_client()
            INSTALL_JOBS_FILE.write_text("not-json", encoding="utf-8")
            original = INSTALL_JOBS_FILE.read_bytes()
            bad = client.get("/api/deps/install-history", headers={"X-Token": TOKEN})
            self.assertEqual(bad.status_code, 500)
            self.assertEqual(INSTALL_JOBS_FILE.read_bytes(), original)

            target = base_dir.parent / f"{base_dir.name}-install-record-target.json"
            target.write_text("{}", encoding="utf-8")
            INSTALL_JOBS_FILE.unlink()
            INSTALL_JOBS_FILE.symlink_to(target)
            linked = client.get("/api/deps/install-history", headers={"X-Token": TOKEN})
            self.assertEqual(linked.status_code, 500)
            self.assertIn("符号链接", linked.json["msg"])
            self.assertEqual(target.read_text(encoding="utf-8"), "{}")
            target.unlink()

    def test_install_history_api_reconciles_live_process_exit_and_persists_terminal_state(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.install_records import INSTALL_JOBS_FILE

            process = SimpleNamespace(
                poll=unittest.mock.Mock(return_value=0),
                terminate=unittest.mock.Mock(), kill=unittest.mock.Mock(),
                wait=unittest.mock.Mock(),
            )
            info, fp = self.seed_running_install(base_dir, "history-exited-install", process=process)
            response = app.test_client().get(
                "/api/deps/install-history?kind=dependency&limit=10",
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(response.status_code, 200)
            item = next(item for item in response.json["items"] if item["id"] == info["id"])
            self.assertEqual(item["status"], "succeeded")
            self.assertEqual(item["status_label"], "安装成功")
            self.assertEqual(item["returncode"], 0)
            self.assertFalse(item["running"])
            self.assertFalse(item["recovery_available"])
            self.assertTrue(fp.closed)
            process.poll.assert_called_once()
            persisted = json.loads(INSTALL_JOBS_FILE.read_text(encoding="utf-8"))
            self.assertFalse(persisted[info["id"]]["running"])
            self.assertEqual(persisted[info["id"]]["status"], "succeeded")

    def test_install_history_filters_are_zero_effect_and_cancel_recovery_is_visible(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.install_records import INSTALL_JOBS_FILE, persist_install_job

            process = SimpleNamespace(
                poll=unittest.mock.Mock(return_value=None),
                terminate=unittest.mock.Mock(), kill=unittest.mock.Mock(),
            )
            info, _fp = self.seed_running_install(base_dir, "history-cancel-pending", process=process)
            info.update(
                cancel_request_id="history-cancel-request", cancel_status="failed",
                cancel_error="fixture terminate denied",
            )
            persist_install_job(info)
            client = app.test_client()
            for query in ("?limit=bad", "?kind=invalid"):
                invalid = client.get("/api/deps/install-history" + query, headers={"X-Token": TOKEN})
                self.assertEqual(invalid.status_code, 400)
            process.poll.assert_not_called()

            active = client.get(
                "/api/deps/install-history?kind=dependency", headers={"X-Token": TOKEN},
            )
            self.assertEqual(active.status_code, 200)
            item = next(item for item in active.json["items"] if item["id"] == info["id"])
            self.assertEqual(item["cancel_status_label"], "取消失败")
            self.assertEqual(item["diagnostic"], "fixture terminate denied")
            self.assertTrue(item["running"])
            process.poll.assert_called_once()

            terminal = dict(info)
            terminal.update(
                running=False, finished=True, status="cancelled", returncode=-15,
                cancel_status="cancelled", cancel_error="", process=None, log_fp=None,
                updated_at="2026-11-18 19:10:00",
            )
            persist_install_job(terminal)
            stored = json.loads(INSTALL_JOBS_FILE.read_text(encoding="utf-8"))[info["id"]]
            self.assertFalse(stored["running"])
            self.assertEqual(stored["status"], "cancelled")
            from fls_manager.state import DEPS_RUNNING
            DEPS_RUNNING.pop(info["id"], None)
            self.assertNotIn(info["id"], DEPS_RUNNING)
            cancelled = client.get(
                "/api/deps/install-history?kind=dependency", headers={"X-Token": TOKEN},
            )
            self.assertEqual(cancelled.status_code, 200)
            stored = json.loads(INSTALL_JOBS_FILE.read_text(encoding="utf-8"))[info["id"]]
            self.assertFalse(stored["running"])
            self.assertEqual(stored["status"], "cancelled")
            cancelled_item = next(item for item in cancelled.json["items"] if item["id"] == info["id"])
            self.assertEqual(cancelled_item["status_label"], "已取消")
            self.assertEqual(cancelled_item["cancel_status_label"], "已取消")
            self.assertTrue(cancelled_item["recovery_available"])
            page = client.get("/deps/install-history?kind=dependency", headers={"X-Token": TOKEN})
            self.assertEqual(page.status_code, 200)
            stored = json.loads(INSTALL_JOBS_FILE.read_text(encoding="utf-8"))[info["id"]]
            self.assertFalse(stored["running"])
            self.assertEqual(stored["status"], "cancelled")
            body = page.get_data(as_text=True)
            self.assertIn("已取消", body)
            self.assertIn("查看日志与恢复", body)
            self.assertIn(f"/deps/install-log/{info['id']}?back=/deps", body)

    def test_install_history_poll_and_persistence_failures_never_claim_success(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.install_records import INSTALL_JOBS_FILE

            process = SimpleNamespace(
                poll=unittest.mock.Mock(side_effect=OSError("fixture poll failed")),
                terminate=unittest.mock.Mock(), kill=unittest.mock.Mock(),
            )
            info, _fp = self.seed_running_install(base_dir, "history-poll-failure", process=process)
            client = app.test_client()
            failed = client.get("/api/deps/install-history", headers={"X-Token": TOKEN})
            self.assertEqual(failed.status_code, 500)
            self.assertIn("fixture poll failed", failed.json["msg"])
            page = client.get("/deps/install-history", headers={"X-Token": TOKEN})
            self.assertEqual(page.status_code, 500)
            self.assertIn("fixture poll failed", page.get_data(as_text=True))
            persisted = json.loads(INSTALL_JOBS_FILE.read_text(encoding="utf-8"))
            self.assertTrue(persisted[info["id"]]["running"])

        with isolated_app() as (app, base_dir):
            from fls_manager.install_records import INSTALL_JOBS_FILE

            process = SimpleNamespace(
                poll=unittest.mock.Mock(return_value=0),
                terminate=unittest.mock.Mock(), kill=unittest.mock.Mock(),
            )
            info, _fp = self.seed_running_install(base_dir, "history-save-failure", process=process)
            with patch(
                "fls_manager.routes.deps.persist_install_job",
                side_effect=OSError("fixture receipt read-only"),
            ):
                response = app.test_client().get(
                    "/api/deps/install-history", headers={"X-Token": TOKEN},
                )
            self.assertEqual(response.status_code, 200)
            item = next(item for item in response.json["items"] if item["id"] == info["id"])
            self.assertEqual(item["status"], "unknown")
            self.assertFalse(item["running"])
            self.assertIn("fixture receipt read-only", item["diagnostic"])
            persisted = json.loads(INSTALL_JOBS_FILE.read_text(encoding="utf-8"))
            self.assertTrue(persisted[info["id"]]["running"])

    def test_running_receipt_failure_keeps_original_install_active_and_recoverable(self):
        for runtime in (False, True):
            with self.subTest(runtime=runtime), isolated_app() as (app, base_dir):
                from fls_manager.install_records import INSTALL_JOBS_FILE, persist_install_job

                install_id = "running-receipt-runtime" if runtime else "running-receipt-dependency"
                process = SimpleNamespace(
                    poll=lambda: None, terminate=unittest.mock.Mock(),
                    wait=unittest.mock.Mock(return_value=-15), kill=unittest.mock.Mock(),
                )
                module = "runtime" if runtime else "deps"
                popen_target = f"fls_manager.routes.{module}.subprocess.Popen"
                endpoint = "/api/install/runtime/node" if runtime else "/api/deps/install"
                payload = {"request_id": install_id}
                if not runtime:
                    payload["name"] = "demo"
                attempts = {"remaining": 1}

                def fail_running_receipt(job):
                    if job["id"] == install_id and job["status"] == "running" and attempts["remaining"]:
                        attempts["remaining"] -= 1
                        raise OSError("running install receipt unavailable")
                    return persist_install_job(job)

                client = app.test_client()
                with patch("fls_manager.routes.deps.persist_install_job", side_effect=fail_running_receipt), patch(
                    popen_target, return_value=process,
                ) as popen, patch(
                    "fls_manager.routes.runtime.runtime_install_command", return_value="echo runtime",
                ):
                    started = client.post(endpoint, json=payload, headers={"X-Token": TOKEN})
                    self.assertEqual(started.status_code, 503)
                    self.assertFalse(started.json["ok"])
                    self.assertTrue(started.json["receipt_pending"])
                    self.assertTrue(started.json["running"])
                    self.assertEqual(started.json["status"], "running")
                    self.assertEqual(started.json["install_id"], install_id)

                    replay = client.post(endpoint, json=payload, headers={"X-Token": TOKEN})
                    self.assertEqual(replay.status_code, 503)
                    self.assertEqual(replay.json["install_id"], install_id)
                    self.assertTrue(replay.json["receipt_pending"])
                    self.assertTrue(replay.json["running"])

                    history = client.get(
                        "/api/deps/install-history?kind=" + ("runtime" if runtime else "dependency"),
                        headers={"X-Token": TOKEN},
                    )
                    self.assertEqual(history.status_code, 200)
                    item = next(item for item in history.json["items"] if item["id"] == install_id)
                    self.assertEqual(item["status"], "running")
                    self.assertTrue(item["receipt_pending"])
                    self.assertFalse(item["recovery_available"])

                    page = client.get(
                        f"/deps/install-log/{install_id}?back=%2Fdeps", headers={"X-Token": TOKEN},
                    )
                    self.assertEqual(page.status_code, 200)
                    body = page.get_data(as_text=True)
                    self.assertIn("安装中，状态回执待保存", body)
                    self.assertIn("running install receipt unavailable", body)
                    self.assertIn(f'data-api-action="/api/deps/install-cancel/{install_id}"', body)

                    recovered = client.get(
                        f"/api/deps/install-log/{install_id}", headers={"X-Token": TOKEN},
                    )
                    self.assertEqual(recovered.status_code, 200)
                    self.assertTrue(recovered.json["running"])
                    self.assertEqual(recovered.json["status"], "running")
                    self.assertNotIn("receipt_pending", recovered.json)
                    disk = json.loads(INSTALL_JOBS_FILE.read_text(encoding="utf-8"))
                    self.assertEqual(disk[install_id]["status"], "running")
                    self.assertTrue(disk[install_id]["running"])

                    final_replay = client.post(endpoint, json=payload, headers={"X-Token": TOKEN})
                    self.assertEqual(final_replay.status_code, 200)
                    self.assertEqual(final_replay.json["install_id"], install_id)
                    popen.assert_called_once()

    def test_retry_child_running_receipt_failure_is_recovered_by_status_query(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.install_records import INSTALL_JOBS_FILE, persist_install_job

            parent_id = "retry-running-receipt-parent"
            child_id = "retry-running-receipt-child"
            log_file = base_dir / "log" / f"deps-install-demo-{parent_id}.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            persist_install_job({
                "id": parent_id, "request_id": parent_id, "kind": "dependency",
                "package": "demo", "log_file": str(log_file), "running": False,
                "finished": True, "status": "failed", "returncode": 1, "error": "failed",
                "start_time": 1, "updated_at": "2026-11-23 01:00:00",
            })
            process = SimpleNamespace(poll=lambda: None)
            attempts = {"remaining": 2}

            def fail_child_running_receipt(job):
                if job["id"] == child_id and job["status"] == "running" and attempts["remaining"]:
                    attempts["remaining"] -= 1
                    raise OSError("retry running receipt unavailable")
                return persist_install_job(job)

            client = app.test_client()
            with patch("fls_manager.routes.deps.persist_install_job", side_effect=fail_child_running_receipt), patch(
                "fls_manager.routes.deps.subprocess.Popen", return_value=process,
            ) as popen:
                started = client.post(
                    f"/api/deps/install-retry/{parent_id}",
                    json={"request_id": child_id}, headers={"X-Token": TOKEN},
                )
                self.assertEqual(started.status_code, 503)
                self.assertTrue(started.json["receipt_pending"])
                self.assertEqual(started.json["install_id"], child_id)

                replay = client.post(
                    f"/api/deps/install-retry/{parent_id}",
                    json={"request_id": child_id}, headers={"X-Token": TOKEN},
                )
                self.assertEqual(replay.status_code, 200)
                self.assertEqual(replay.json["install_id"], child_id)
                self.assertTrue(replay.json["running"])
                popen.assert_called_once()
                disk = json.loads(INSTALL_JOBS_FILE.read_text(encoding="utf-8"))
                self.assertEqual(disk[child_id]["status"], "running")
                self.assertTrue(disk[child_id]["running"])

    def test_cancel_repairs_running_receipt_before_signaling_original_process(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.install_records import INSTALL_JOBS_FILE, persist_install_job

            install_id = "cancel-pending-running-receipt"
            process = SimpleNamespace(
                poll=lambda: None, terminate=unittest.mock.Mock(),
                wait=unittest.mock.Mock(return_value=-15), kill=unittest.mock.Mock(),
            )
            attempts = {"remaining": 1}

            def fail_running_receipt(job):
                if job["id"] == install_id and job["status"] == "running" and attempts["remaining"]:
                    attempts["remaining"] -= 1
                    raise OSError("running install receipt unavailable")
                return persist_install_job(job)

            client = app.test_client()
            with patch("fls_manager.routes.deps.persist_install_job", side_effect=fail_running_receipt), patch(
                "fls_manager.routes.deps.subprocess.Popen", return_value=process,
            ) as popen:
                started = client.post(
                    "/api/deps/install", json={"name": "demo", "request_id": install_id},
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(started.status_code, 503)
                self.assertTrue(started.json["receipt_pending"])

                cancelled = client.post(
                    f"/api/deps/install-cancel/{install_id}",
                    json={"request_id": "cancel-after-receipt-repair"}, headers={"X-Token": TOKEN},
                )

            self.assertEqual(cancelled.status_code, 200)
            self.assertEqual(cancelled.json["status"], "cancelled")
            self.assertEqual(cancelled.json["cancel_status"], "cancelled")
            process.terminate.assert_called_once()
            process.wait.assert_called_once_with(timeout=3)
            process.kill.assert_not_called()
            popen.assert_called_once()
            disk = json.loads(INSTALL_JOBS_FILE.read_text(encoding="utf-8"))
            self.assertEqual(disk[install_id]["status"], "cancelled")
            self.assertFalse(disk[install_id]["running"])
            self.assertEqual(disk[install_id]["cancel_request_id"], "cancel-after-receipt-repair")

    def test_restart_unknown_install_requires_confirmation_before_retry(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.install_records import INSTALL_JOBS_FILE, persist_install_job
            from fls_manager.state import DEPS_RUNNING

            install_id = "restart-running-receipt"
            original_process = SimpleNamespace(poll=lambda: None)
            retry_process = SimpleNamespace(poll=lambda: None)
            attempts = {"remaining": 1}

            def fail_running_receipt(job):
                if job["id"] == install_id and job["status"] == "running" and attempts["remaining"]:
                    attempts["remaining"] -= 1
                    raise OSError("running install receipt unavailable")
                return persist_install_job(job)

            client = app.test_client()
            with patch("fls_manager.routes.deps.persist_install_job", side_effect=fail_running_receipt), patch(
                "fls_manager.routes.deps.subprocess.Popen",
                side_effect=[original_process, retry_process],
            ) as popen:
                started = client.post(
                    "/api/deps/install", json={"name": "demo", "request_id": install_id},
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(started.status_code, 503)
                self.assertEqual(popen.call_count, 1)

                orphan = DEPS_RUNNING.pop(install_id)
                orphan["log_fp"].close()
                original_process.poll = lambda: 1

                page = client.get(
                    f"/deps/install-log/{install_id}?back=%2Fdeps", headers={"X-Token": TOKEN},
                )
                self.assertEqual(page.status_code, 200)
                body = page.get_data(as_text=True)
                self.assertIn("面板重启后无法从原进程句柄确认", body)
                self.assertIn("install-recovery-confirm-form", body)
                self.assertNotIn('class="inline-form install-retry-form"', body)
                csrf = re.search(r'name="csrf_token" value="([^"]+)"', body).group(1)
                unauthorized = client.post(
                    f"/api/deps/install-recovery-confirm/{install_id}",
                    json={"request_id": install_id, "confirm_not_running": True},
                    headers={"X-CSRF-Token": csrf},
                )
                self.assertEqual(unauthorized.status_code, 401)

                invalid_confirmation = client.post(
                    f"/deps/install-recovery-confirm/{install_id}",
                    data={"request_id": install_id, "confirm_not_running": "true", "csrf_token": "wrong"},
                )
                self.assertEqual(invalid_confirmation.status_code, 400)
                invalid_choice = client.post(
                    f"/api/deps/install-recovery-confirm/{install_id}",
                    json={"request_id": install_id, "confirm_not_running": False},
                    headers={"X-Token": TOKEN},
                )
                wrong_identity = client.post(
                    f"/api/deps/install-recovery-confirm/{install_id}",
                    json={"request_id": "different-install", "confirm_not_running": True},
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(invalid_choice.status_code, 400)
                self.assertEqual(wrong_identity.status_code, 400)
                with client.session_transaction() as session:
                    session.update(
                        token=TOKEN, token_expire_at=9999999999,
                        security_verified=True, csrf_token=csrf,
                    )

                history = client.get(
                    "/api/deps/install-history?kind=dependency", headers={"X-Token": TOKEN},
                )
                item = next(item for item in history.json["items"] if item["id"] == install_id)
                self.assertEqual(item["status"], "unknown")
                self.assertTrue(item["retry_confirmation_required"])
                self.assertFalse(item["recovery_available"])
                persisted = json.loads(INSTALL_JOBS_FILE.read_text(encoding="utf-8"))
                self.assertTrue(persisted[install_id]["retry_confirmation_required"])

                blocked = client.post(
                    f"/api/deps/install-retry/{install_id}",
                    json={"request_id": "retry-before-confirmation"}, headers={"X-Token": TOKEN},
                )
                self.assertEqual(blocked.status_code, 409)
                self.assertTrue(blocked.json["confirmation_required"])
                from fls_manager.install_records import get_install_job
                with patch(
                    "fls_manager.routes.deps.get_install_job",
                    return_value={**get_install_job(install_id), "retry_confirmation_required": False},
                ):
                    raced = client.post(
                        f"/api/deps/install-retry/{install_id}",
                        json={"request_id": "retry-racing-confirmation"}, headers={"X-Token": TOKEN},
                    )
                self.assertEqual(raced.status_code, 409)
                self.assertTrue(raced.json["confirmation_required"])
                popen.assert_called_once()

                confirmed = client.post(
                    f"/deps/install-recovery-confirm/{install_id}",
                    data={"request_id": install_id, "confirm_not_running": "true", "csrf_token": csrf},
                )
                self.assertEqual(confirmed.status_code, 302)
                repeated = client.post(
                    f"/api/deps/install-recovery-confirm/{install_id}",
                    json={"request_id": install_id, "confirm_not_running": True},
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(repeated.status_code, 200)
                self.assertFalse(repeated.json["retry_confirmation_required"])

                retried = client.post(
                    f"/api/deps/install-retry/{install_id}",
                    json={"request_id": "retry-after-confirmation"}, headers={"X-Token": TOKEN},
                )
                self.assertEqual(retried.status_code, 200)
                self.assertEqual(retried.json["install_id"], "retry-after-confirmation")
                self.assertEqual(popen.call_count, 2)
                disk = json.loads(INSTALL_JOBS_FILE.read_text(encoding="utf-8"))
                self.assertTrue(disk[install_id]["retried_by"])

    def test_retry_confirmation_write_failure_stays_blocked_and_published_error_recovers(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.install_records import (
                INSTALL_JOBS_FILE, get_install_job, persist_install_job,
            )
            install_id = "confirm-write-recovery"
            log_file = base_dir / "log" / f"deps-install-demo-{install_id}.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            persist_install_job({
                "id": install_id, "request_id": install_id, "kind": "dependency",
                "package": "demo", "log_file": str(log_file), "running": True,
                "finished": False, "status": "running", "returncode": None,
                "error": "", "start_time": 1, "updated_at": "2026-11-18 19:01:00",
            })
            recovered = get_install_job(install_id)
            self.assertEqual(recovered["status"], "unknown")
            self.assertTrue(recovered["retry_confirmation_required"])
            client = app.test_client()
            confirm_url = f"/api/deps/install-recovery-confirm/{install_id}"
            confirm_payload = {"request_id": install_id, "confirm_not_running": True}
            with patch("fls_manager.install_records.mutate_json", side_effect=OSError("disk busy")), patch(
                "fls_manager.routes.deps.subprocess.Popen"
            ) as popen:
                failed = client.post(confirm_url, json=confirm_payload, headers={"X-Token": TOKEN})
                blocked = client.post(
                    f"/api/deps/install-retry/{install_id}",
                    json={"request_id": "retry-confirm-save-failed"}, headers={"X-Token": TOKEN},
                )
            self.assertEqual(failed.status_code, 503)
            self.assertTrue(failed.json["retry_confirmation_required"])
            self.assertEqual(blocked.status_code, 409)
            popen.assert_not_called()
            persisted = json.loads(INSTALL_JOBS_FILE.read_text(encoding="utf-8"))
            self.assertTrue(persisted[install_id]["retry_confirmation_required"])

            from fls_manager import install_records
            mutate_json = install_records.mutate_json

            def publish_then_raise(*args, **kwargs):
                mutate_json(*args, **kwargs)
                raise OSError("confirmation acknowledgement lost")

            with patch("fls_manager.install_records.mutate_json", side_effect=publish_then_raise):
                recovered_confirmation = client.post(confirm_url, json=confirm_payload, headers={"X-Token": TOKEN})
            self.assertEqual(recovered_confirmation.status_code, 200)
            self.assertFalse(recovered_confirmation.json["retry_confirmation_required"])
            repeated = client.post(confirm_url, json=confirm_payload, headers={"X-Token": TOKEN})
            self.assertEqual(repeated.status_code, 200)
            self.assertFalse(repeated.json["retry_confirmation_required"])
            self.assertFalse(json.loads(INSTALL_JOBS_FILE.read_text(encoding="utf-8"))[install_id]["retry_confirmation_required"])

    def test_terminal_install_receipt_failure_is_retryable_without_reinstalling(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.install_records import INSTALL_JOBS_FILE, persist_install_job

            process = SimpleNamespace(poll=lambda: 0)
            info, _fp = self.seed_running_install(base_dir, "terminal-receipt-recovery", process=process)
            client = app.test_client()
            attempts = {"remaining": 1}

            def fail_once(job):
                if job["id"] == info["id"] and attempts["remaining"]:
                    attempts["remaining"] -= 1
                    raise OSError("terminal receipt temporarily unavailable")
                return persist_install_job(job)

            with patch("fls_manager.routes.deps.persist_install_job", side_effect=fail_once):
                failed = client.get(
                    f"/api/deps/install-log/{info['id']}", headers={"X-Token": TOKEN},
                )
                self.assertEqual(failed.status_code, 503)
                self.assertTrue(failed.json["receipt_pending"])
                self.assertEqual(failed.json["status"], "unknown")
                self.assertIn("terminal receipt temporarily unavailable", failed.json["error"])

                history = client.get(
                    "/api/deps/install-history?kind=dependency&limit=10",
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(history.status_code, 200)
                item = next(item for item in history.json["items"] if item["id"] == info["id"])
                self.assertEqual(item["status"], "unknown")
                self.assertFalse(item["recovery_available"])
                self.assertIn("terminal receipt temporarily unavailable", item["diagnostic"])

                retry = client.post(
                    f"/api/deps/install-retry/{info['id']}",
                    json={"request_id": "must-not-retry-install"},
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(retry.status_code, 503)
                self.assertTrue(retry.json["receipt_pending"])

                persisted = json.loads(INSTALL_JOBS_FILE.read_text(encoding="utf-8"))
                self.assertEqual(persisted[info["id"]]["status"], "running")
                self.assertTrue(persisted[info["id"]]["running"])

                recovered = client.get(
                    f"/api/deps/install-log/{info['id']}", headers={"X-Token": TOKEN},
                )
                self.assertEqual(recovered.status_code, 200)
                self.assertEqual(recovered.json["status"], "succeeded")
                self.assertFalse(recovered.json["running"])
                self.assertNotIn("receipt_pending", recovered.json)
                persisted = json.loads(INSTALL_JOBS_FILE.read_text(encoding="utf-8"))
                self.assertEqual(persisted[info["id"]]["status"], "succeeded")
                self.assertFalse(persisted[info["id"]]["running"])

            with patch("fls_manager.routes.deps.subprocess.Popen") as popen:
                denied_retry = client.post(
                    f"/api/deps/install-retry/{info['id']}",
                    json={"request_id": "must-not-retry-install"},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(denied_retry.status_code, 409)
            popen.assert_not_called()

    def test_cancel_receipt_failure_recovers_by_original_cancel_id_without_resignal(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.install_records import INSTALL_JOBS_FILE, persist_install_job

            process = SimpleNamespace(
                returncode=-15, poll=lambda: None,
                terminate=unittest.mock.Mock(), kill=unittest.mock.Mock(),
                wait=unittest.mock.Mock(return_value=-15),
            )
            info, _fp = self.seed_running_install(
                base_dir, "cancel-terminal-receipt-recovery", process=process,
            )
            client = app.test_client()
            attempts = {"remaining": 2}

            def fail_twice(job):
                if job["id"] == info["id"] and attempts["remaining"]:
                    attempts["remaining"] -= 1
                    raise OSError("cancel receipt temporarily unavailable")
                return persist_install_job(job)

            with patch("fls_manager.routes.deps.persist_install_job", side_effect=fail_twice):
                stopped = client.post(
                    f"/api/deps/install-cancel/{info['id']}",
                    json={"request_id": "stable-cancel-recovery"},
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(stopped.status_code, 503)
                self.assertTrue(stopped.json["receipt_pending"])
                self.assertEqual(stopped.json["request_id"], "stable-cancel-recovery")
                process.terminate.assert_called_once()

                unavailable = client.get(
                    f"/api/deps/install-cancel/{info['id']}/stable-cancel-recovery",
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(unavailable.status_code, 503)
                self.assertEqual(unavailable.json["request_id"], "stable-cancel-recovery")
                self.assertTrue(unavailable.json["receipt_pending"])
                disk = json.loads(INSTALL_JOBS_FILE.read_text(encoding="utf-8"))
                self.assertEqual(disk[info["id"]]["cancel_status"], "requested")

                recovered = client.get(
                    f"/api/deps/install-cancel/{info['id']}/stable-cancel-recovery",
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(recovered.status_code, 200)
                self.assertEqual(recovered.json["cancel_status"], "cancelled")
                self.assertFalse(recovered.json["running"])
                disk = json.loads(INSTALL_JOBS_FILE.read_text(encoding="utf-8"))
                self.assertEqual(disk[info["id"]]["cancel_status"], "cancelled")

                replay = client.post(
                    f"/api/deps/install-cancel/{info['id']}",
                    json={"request_id": "stable-cancel-recovery"},
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(replay.status_code, 200)
                self.assertEqual(replay.json["cancel_status"], "cancelled")

            process.terminate.assert_called_once()
            process.wait.assert_called_once_with(timeout=3)
            process.kill.assert_not_called()

    def test_published_terminal_receipt_exception_is_confirmed_without_repeating_actions(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.install_records import persist_install_job

            process = SimpleNamespace(poll=lambda: 0)
            info, _fp = self.seed_running_install(
                base_dir, "published-install-receipt", process=process,
            )
            client = app.test_client()

            def publish_then_raise(job):
                result = persist_install_job(job)
                if job["id"] == info["id"] and not job.get("running"):
                    raise OSError("install receipt acknowledgement lost")
                return result

            with patch("fls_manager.routes.deps.persist_install_job", side_effect=publish_then_raise):
                response = client.get(
                    f"/api/deps/install-log/{info['id']}", headers={"X-Token": TOKEN},
                )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["status"], "succeeded")
            self.assertFalse(response.json["running"])

        with isolated_app() as (app, base_dir):
            from fls_manager.install_records import persist_install_job

            process = SimpleNamespace(
                poll=lambda: None,
                terminate=unittest.mock.Mock(),
                wait=unittest.mock.Mock(return_value=-15),
                kill=unittest.mock.Mock(),
            )
            info, _fp = self.seed_running_install(
                base_dir, "published-cancel-receipt", process=process,
            )

            def publish_cancel_then_raise(job):
                result = persist_install_job(job)
                if job["id"] == info["id"] and job.get("cancel_status") == "cancelled":
                    raise OSError("cancel receipt acknowledgement lost")
                return result

            client = app.test_client()
            with patch("fls_manager.routes.deps.persist_install_job", side_effect=publish_cancel_then_raise):
                stopped = client.post(
                    f"/api/deps/install-cancel/{info['id']}",
                    json={"request_id": "published-cancel-id"},
                    headers={"X-Token": TOKEN},
                )
                replay = client.post(
                    f"/api/deps/install-cancel/{info['id']}",
                    json={"request_id": "published-cancel-id"},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(stopped.status_code, 200)
            self.assertEqual(stopped.json["cancel_status"], "cancelled")
            self.assertEqual(replay.status_code, 200)
            process.terminate.assert_called_once()

    def test_install_history_exposes_verified_retry_parent_and_child_links(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.install_records import persist_install_job

            parent_id = "history-retry-parent"
            child_id = "history-retry-child"
            parent_log = base_dir / "log" / f"deps-install-demo-{parent_id}.log"
            child_log = base_dir / "log" / f"deps-install-demo-{child_id}.log"
            parent_log.parent.mkdir(parents=True, exist_ok=True)
            parent_log.write_text("parent failure\n", encoding="utf-8")
            child_log.write_text("retry failure\n", encoding="utf-8")
            persist_install_job({
                "id": parent_id, "request_id": parent_id, "kind": "dependency",
                "package": "demo", "log_file": str(parent_log), "running": False,
                "finished": True, "status": "failed", "returncode": 1,
                "error": "parent failed", "start_time": 1, "updated_at": "2026-11-18 20:00:00",
            })
            persist_install_job({
                "id": child_id, "request_id": child_id, "kind": "dependency",
                "retry_of": parent_id, "package": "demo", "log_file": str(child_log),
                "running": False, "finished": True, "status": "failed", "returncode": 2,
                "error": "retry failed", "start_time": 2, "updated_at": "2026-11-18 20:01:00",
            })
            client = app.test_client()
            limited = client.get(
                "/api/deps/install-history?kind=dependency&limit=1", headers={"X-Token": TOKEN},
            )
            self.assertEqual(limited.status_code, 200)
            parent = limited.json["items"][0]
            self.assertEqual(parent["id"], parent_id)
            self.assertEqual(parent["retry_relations"][0]["relation"], "retried_by")
            self.assertEqual(parent["retry_relations"][0]["id"], child_id)
            self.assertEqual(parent["retry_relations"][0]["status_label"], "安装失败")
            self.assertFalse(parent["recovery_available"])

            complete = client.get(
                "/api/deps/install-history?kind=dependency&limit=10", headers={"X-Token": TOKEN},
            )
            items = {item["id"]: item for item in complete.json["items"]}
            parent = items[parent_id]
            child = items[child_id]
            self.assertEqual(parent["retry_relations"][0]["relation"], "retried_by")
            self.assertEqual(parent["retry_relations"][0]["id"], child_id)
            self.assertFalse(parent["recovery_available"])
            self.assertEqual(child["retry_relations"][0]["id"], parent_id)
            page = client.get("/deps/install-history?kind=dependency", headers={"X-Token": TOKEN})
            body = page.get_data(as_text=True)
            self.assertIn(f"/deps/install-log/{parent_id}?back=/deps/install-history", body)
            self.assertIn(f"/deps/install-log/{child_id}?back=/deps/install-history", body)
            self.assertIn("后续重试", body)
            self.assertIn("原安装", body)
            self.assertIn("<th>退出码</th>", body)
            self.assertIn("<td>2</td>", body)
            parent_log_page = client.get(
                f"/deps/install-log/{parent_id}", headers={"X-Token": TOKEN},
            )
            parent_log_body = parent_log_page.get_data(as_text=True)
            self.assertNotIn('<form class="inline-form install-retry-form"', parent_log_body)
            self.assertIn("查看已提交重试", parent_log_body)
            self.assertIn(f"/deps/install-log/{child_id}?back=/deps/install-history", parent_log_body)
            child_log_page = client.get(
                f"/deps/install-log/{child_id}", headers={"X-Token": TOKEN},
            )
            self.assertIn('<form class="inline-form install-retry-form"', child_log_page.get_data(as_text=True))

    def test_install_history_broken_retry_links_are_diagnostic_and_not_retryable(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.install_records import INSTALL_JOBS_FILE, persist_install_job

            parent_id = "history-broken-parent"
            log_file = base_dir / "log" / f"deps-install-demo-{parent_id}.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            persist_install_job({
                "id": parent_id, "request_id": parent_id, "kind": "dependency",
                "package": "demo", "log_file": str(log_file), "running": False,
                "finished": True, "status": "failed", "returncode": 1,
                "error": "original failed", "retried_by": "missing-retry-child",
                "start_time": 1, "updated_at": "2026-11-18 20:02:00",
            })
            client = app.test_client()
            item = client.get(
                "/api/deps/install-history", headers={"X-Token": TOKEN},
            ).json["items"][0]
            self.assertFalse(item["recovery_available"])
            self.assertEqual(item["retry_relations"], [])
            self.assertIn("缺失或不匹配", item["retry_relation_diagnostic"])
            page = client.get("/deps/install-history", headers={"X-Token": TOKEN})
            self.assertIn("missing-retry-child", page.get_data(as_text=True))

            with patch("fls_manager.routes.deps.subprocess.Popen") as popen:
                retry = client.post(
                    f"/api/deps/install-retry/{parent_id}",
                    json={"request_id": "history-repair-attempt"}, headers={"X-Token": TOKEN},
                )
            self.assertEqual(retry.status_code, 409)
            popen.assert_not_called()

            records = json.loads(INSTALL_JOBS_FILE.read_text(encoding="utf-8"))
            records["other-retry-parent"] = {
                "id": "other-retry-parent", "request_id": "other-retry-parent",
                "kind": "dependency", "package": "demo",
                "log_file": str(base_dir / "log" / "other.log"), "running": False,
                "status": "failed", "returncode": 1, "error": "other failure",
                "updated_at": "2026-11-18 20:03:00", "retry_of": "", "retried_by": "mismatched-child",
            }
            records["mismatched-child"] = {
                "id": "mismatched-child", "request_id": "mismatched-child",
                "kind": "dependency", "package": "demo",
                "log_file": str(base_dir / "log" / "child.log"), "running": False,
                "status": "failed", "returncode": 2, "error": "child failure",
                "updated_at": "2026-11-18 20:04:00", "retry_of": "other-retry-parent", "retried_by": "",
            }
            records[parent_id]["retried_by"] = "mismatched-child"
            INSTALL_JOBS_FILE.write_text(json.dumps(records), encoding="utf-8")
            mismatched = client.get(
                "/api/deps/install-history?limit=100", headers={"X-Token": TOKEN},
            )
            primary = next(info for info in mismatched.json["items"] if info["id"] == parent_id)
            self.assertEqual(primary["retry_relations"], [])
            self.assertIn("缺失或不匹配", primary["retry_relation_diagnostic"])
            self.assertFalse(primary["recovery_available"])
            with patch("fls_manager.routes.deps.subprocess.Popen") as popen:
                mismatch_retry = client.post(
                    f"/api/deps/install-retry/{parent_id}",
                    json={"request_id": "history-mismatch-retry"}, headers={"X-Token": TOKEN},
                )
            self.assertEqual(mismatch_retry.status_code, 409)
            popen.assert_not_called()

    def test_pruned_retry_parent_blocks_history_and_log_retry_actions(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.install_records import INSTALL_JOBS_FILE

            child_id = "orphaned-retry-child"
            log_file = base_dir / "log" / f"deps-install-demo-{child_id}.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text("orphaned retry failed\n", encoding="utf-8")
            records = {
                child_id: {
                    "id": child_id, "request_id": child_id, "kind": "dependency",
                    "package": "demo", "log_file": str(log_file), "running": False,
                    "status": "failed", "returncode": 3, "error": "child failed",
                    "updated_at": "2026-11-18 21:00:00", "retry_of": "pruned-retry-parent",
                    "retried_by": "", "cancel_request_id": "", "cancel_status": "",
                    "cancel_error": "",
                },
            }
            for index in range(99):
                install_id = f"retained-history-{index:03d}"
                records[install_id] = {
                    "id": install_id, "request_id": install_id, "kind": "dependency",
                    "package": "demo", "log_file": str(base_dir / "log" / f"{install_id}.log"),
                    "running": False, "status": "succeeded", "returncode": 0,
                    "error": "", "updated_at": "2026-11-18 20:00:00",
                    "retry_of": "", "retried_by": "", "cancel_request_id": "",
                    "cancel_status": "", "cancel_error": "",
                }
            INSTALL_JOBS_FILE.write_text(json.dumps(records), encoding="utf-8")
            client = app.test_client()
            history = client.get(
                "/api/deps/install-history?limit=100", headers={"X-Token": TOKEN},
            )
            self.assertEqual(history.status_code, 200)
            orphan = next(item for item in history.json["items"] if item["id"] == child_id)
            self.assertFalse(orphan["recovery_available"])
            self.assertIn("pruned-retry-parent", orphan["retry_relation_diagnostic"])
            self.assertIn("缺失或不匹配", orphan["retry_relation_diagnostic"])

            log_page = client.get(
                f"/deps/install-log/{child_id}?back=%2Fdeps%2Finstall-history",
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(log_page.status_code, 200)
            log_body = log_page.get_data(as_text=True)
            self.assertNotIn('<form class="inline-form install-retry-form"', log_body)
            self.assertIn("不能安全启动后续重试", log_body)
            retry_status = client.get(
                f"/api/deps/install-retry/{child_id}/orphan-retry-request",
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(retry_status.status_code, 409)
            with patch("fls_manager.routes.deps.subprocess.Popen") as popen:
                retry = client.post(
                    f"/api/deps/install-retry/{child_id}",
                    json={"request_id": "orphan-retry-request"}, headers={"X-Token": TOKEN},
                )
            self.assertEqual(retry.status_code, 409)
            popen.assert_not_called()

    def test_install_history_retains_active_retry_chain_as_one_capacity_unit(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.install_records import INSTALL_JOB_LIMIT, INSTALL_JOBS_FILE, persist_install_job

            records = {}
            for index in range(INSTALL_JOB_LIMIT - 1):
                install_id = f"old-independent-{index:03d}"
                records[install_id] = {
                    "id": install_id, "request_id": install_id, "kind": "dependency",
                    "package": "demo", "log_file": str(base_dir / "log" / f"{install_id}.log"),
                    "running": False, "status": "succeeded", "returncode": 0,
                    "error": "", "updated_at": "2026-11-18 18:00:00",
                }
            parent_id, child_id = "capacity-chain-parent", "capacity-chain-child"
            records[parent_id] = {
                "id": parent_id, "request_id": parent_id, "kind": "dependency",
                "package": "demo", "log_file": str(base_dir / "log" / f"{parent_id}.log"),
                "running": False, "status": "failed", "returncode": 1,
                "error": "parent failed", "updated_at": "2026-11-18 19:00:00",
                "retried_by": child_id,
            }
            INSTALL_JOBS_FILE.write_text(json.dumps(records), encoding="utf-8")
            child_log = base_dir / "log" / f"{child_id}.log"
            persist_install_job({
                "id": child_id, "request_id": child_id, "kind": "dependency",
                "retry_of": parent_id, "package": "demo", "log_file": str(child_log),
                "running": True, "status": "prepared", "returncode": None,
                "error": "", "start_time": 1, "updated_at": "2026-11-18 21:00:00",
            })
            persisted = json.loads(INSTALL_JOBS_FILE.read_text(encoding="utf-8"))
            self.assertEqual(len(persisted), INSTALL_JOB_LIMIT)
            self.assertIn(parent_id, persisted)
            self.assertIn(child_id, persisted)
            self.assertEqual(persisted[parent_id]["retried_by"], child_id)
            self.assertEqual(persisted[child_id]["retry_of"], parent_id)
            self.assertEqual(sum(install_id.startswith("old-independent-") for install_id in persisted), INSTALL_JOB_LIMIT - 2)

    def test_install_history_evicts_old_retry_chain_together(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.install_records import INSTALL_JOB_LIMIT, INSTALL_JOBS_FILE, persist_install_job

            parent_id, child_id = "old-chain-parent", "old-chain-child"
            records = {
                parent_id: {
                    "id": parent_id, "request_id": parent_id, "kind": "dependency",
                    "package": "demo", "log_file": str(base_dir / "log" / f"{parent_id}.log"),
                    "running": False, "status": "failed", "returncode": 1,
                    "error": "parent failed", "updated_at": "2026-11-18 18:00:00",
                    "retried_by": child_id,
                },
                child_id: {
                    "id": child_id, "request_id": child_id, "kind": "dependency",
                    "package": "demo", "log_file": str(base_dir / "log" / f"{child_id}.log"),
                    "running": False, "status": "failed", "returncode": 2,
                    "error": "child failed", "updated_at": "2026-11-18 18:00:00",
                    "retry_of": parent_id,
                },
            }
            for index in range(INSTALL_JOB_LIMIT - 2):
                install_id = f"newer-independent-{index:03d}"
                records[install_id] = {
                    "id": install_id, "request_id": install_id, "kind": "dependency",
                    "package": "demo", "log_file": str(base_dir / "log" / f"{install_id}.log"),
                    "running": False, "status": "succeeded", "returncode": 0,
                    "error": "", "updated_at": "2026-11-18 19:00:00",
                }
            INSTALL_JOBS_FILE.write_text(json.dumps(records), encoding="utf-8")
            for install_id in ("newer-after-fill-a", "newer-after-fill-b"):
                persist_install_job({
                    "id": install_id, "request_id": install_id, "kind": "dependency",
                    "package": "demo", "log_file": str(base_dir / "log" / f"{install_id}.log"),
                    "running": False, "status": "succeeded", "returncode": 0,
                    "error": "", "updated_at": "2026-11-18 20:00:00",
                })
            persisted = json.loads(INSTALL_JOBS_FILE.read_text(encoding="utf-8"))
            self.assertEqual(len(persisted), INSTALL_JOB_LIMIT)
            self.assertNotIn(parent_id, persisted)
            self.assertNotIn(child_id, persisted)
            self.assertFalse(any(
                info.get("retry_of") == parent_id or info.get("retried_by") == child_id
                for info in persisted.values()
            ))

    def test_retry_claim_refuses_a_chain_larger_than_history_capacity(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.install_records import INSTALL_JOB_LIMIT, INSTALL_JOBS_FILE

            records = {}
            for index in range(INSTALL_JOB_LIMIT):
                install_id = f"oversized-chain-{index:03d}"
                records[install_id] = {
                    "id": install_id, "request_id": install_id, "kind": "dependency",
                    "package": "demo", "log_file": str(base_dir / "log" / f"{install_id}.log"),
                    "running": False, "status": "failed", "returncode": 1,
                    "error": "failed", "updated_at": f"2026-11-18 20:{index // 60:02d}:{index % 60:02d}",
                    "retry_of": f"oversized-chain-{index - 1:03d}" if index else "",
                    "retried_by": f"oversized-chain-{index + 1:03d}" if index < INSTALL_JOB_LIMIT - 1 else "",
                }
            INSTALL_JOBS_FILE.write_text(json.dumps(records), encoding="utf-8")
            original_bytes = INSTALL_JOBS_FILE.read_bytes()
            with patch("fls_manager.routes.deps.subprocess.Popen") as popen:
                response = app.test_client().post(
                    f"/api/deps/install-retry/oversized-chain-{INSTALL_JOB_LIMIT - 1:03d}",
                    json={"request_id": "oversized-chain-new-child"}, headers={"X-Token": TOKEN},
                )
            self.assertEqual(response.status_code, 409)
            self.assertIn("超过历史保留上限", response.json["msg"])
            popen.assert_not_called()
            self.assertEqual(INSTALL_JOBS_FILE.read_bytes(), original_bytes)

    def test_install_log_rejects_outside_and_symlink_paths_without_reading_them(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.install_records import INSTALL_JOBS_FILE

            outside = base_dir.parent / f"{base_dir.name}-outside-deps.log"
            outside.write_text("outside dependency secret\n", encoding="utf-8")
            record = {
                "id": "unsafe-dependency", "request_id": "unsafe-dependency",
                "kind": "dependency", "package": "demo", "log_file": str(outside),
                "start_time": 1, "running": False, "status": "failed",
                "returncode": 1, "error": "failed", "updated_at": "2026-11-18 16:00:00",
                "retry_of": "", "retried_by": "",
            }
            INSTALL_JOBS_FILE.write_text(json.dumps({record["id"]: record}), encoding="utf-8")
            client = app.test_client()
            response = client.get(
                "/api/deps/install-log/unsafe-dependency", headers={"X-Token": TOKEN},
            )
            self.assertEqual(response.status_code, 500)
            self.assertIn("受控日志目录", response.json["error"])
            self.assertEqual(outside.read_text(encoding="utf-8"), "outside dependency secret\n")

            target = base_dir / "real-dependency.log"
            target.write_text("target\n", encoding="utf-8")
            link = base_dir / "log" / "deps-install-demo-unsafe-dependency.log"
            link.symlink_to(target)
            record["log_file"] = str(link)
            INSTALL_JOBS_FILE.write_text(json.dumps({record["id"]: record}), encoding="utf-8")
            linked = client.get(
                "/api/deps/install-log/unsafe-dependency", headers={"X-Token": TOKEN},
            )
            self.assertEqual(linked.status_code, 500)
            self.assertIn("受控日志目录", linked.json["error"])
            self.assertEqual(target.read_text(encoding="utf-8"), "target\n")
            outside.unlink()

    def test_install_log_api_keeps_auth_boundary_and_running_state(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.state import DEPS_RUNNING

            log_file = base_dir / "log" / "deps-install-demo-demo-id.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text("already installed output\n", encoding="utf-8")
            DEPS_RUNNING["demo-id"] = {
                "kind": "dependency",
                "package": "demo",
                "log_file": str(log_file),
                "process": SimpleNamespace(poll=lambda: None),
                "finished": False,
                "returncode": None,
                "log_fp": None,
            }

            client = app.test_client()
            denied = client.get("/api/deps/install-log/demo-id")
            self.assertEqual(denied.status_code, 401)
            self.assertEqual(denied.get_json()["msg"], "登录已过期")

            response = client.get(
                "/api/deps/install-log/demo-id", headers={"X-Token": TOKEN},
            )
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.get_json()["running"])
            self.assertEqual(response.get_json()["log"], "already installed output")

            page = client.get(
                "/deps/install-log/demo-id", headers={"X-Token": TOKEN},
            ).get_data(as_text=True)
            for control_id in ("installPollError", "installPollRetry", "installPollLogin"):
                self.assertIn(f'id="{control_id}"', page)
            self.assertIn('id="installPollLogin" class="btn btn-primary" hidden>重新登录</a>', page)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise log polling")
    def test_polling_preserves_output_and_recovers_from_http_and_auth_errors(self):
        with isolated_app() as (app, _base_dir):
            html = app.test_client().get(
                "/deps/install-log/demo-id", headers={"X-Token": TOKEN},
            ).get_data(as_text=True)
            script = next(
                item for item in re.findall(r"<script>(.*?)</script>", html, re.S)
                if "async function loadLog()" in item
            )
            source = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
            helpers = source[
                source.index("function flsResponseErrorDiagnostic("):
                source.index("async function flsReadTextResponse(")
            ]
            result = subprocess.run(
                ["node", "-e", r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));

function makeContext(responses) {
  const elements = Object.fromEntries([
    'installPollError', 'installPollRetry', 'installPollLogin', 'installStatus',
    'installReturncode', 'installError', 'log'
  ].map(id => [id, {
    textContent: '', hidden: id === 'installPollError' || id === 'installPollRetry' || id === 'installPollLogin',
    href: '', listeners: {}, addEventListener(name, callback) { this.listeners[name] = callback; }
  }]));
  elements.log.textContent = 'previously read output';
  elements.installStatus.textContent = '安装中';
  let nextTimer = 1;
  const timers = new Map();
  const context = {
    window: {scrollTo() {}},
    document: {documentElement: {scrollHeight: 100}, getElementById: id => elements[id]},
    location: {pathname: '/deps/install-log/demo-id', search: '?back=%2Fdeps'},
    fetch: async () => responses.shift(),
    setInterval(callback, delay) { const id = nextTimer++; timers.set(id, {callback, delay}); return id; },
    clearInterval(id) { timers.delete(id); }
  };
  vm.createContext(context);
  vm.runInContext(input.helpers + input.script, context);
  return {context, elements, timers};
}

function response(status, body, contentType) {
  return {ok: status >= 200 && status < 300, status,
    headers: {get: () => contentType}, text: async () => body};
}
const tick = () => new Promise(resolve => setImmediate(resolve));

(async () => {
  const transient = makeContext([
    response(200, JSON.stringify({running: true, status_label: '安装中', returncode: null,
      error: '', log: 'previously read output'}), 'application/json'),
    response(502, 'upstream temporarily unavailable', 'text/plain'),
    response(200, JSON.stringify({running: true, status_label: '安装中', returncode: null,
      error: '', log: 'latest output'}), 'application/json')
  ]);
  await tick();
  assert.equal(transient.elements.log.textContent, 'previously read output');
  const transientTimer = [...transient.timers.values()][0];
  await transientTimer.callback();
  assert.equal(transient.elements.log.textContent, 'previously read output');
  assert.equal(transient.elements.installStatus.textContent, '安装中');
  assert.match(transient.elements.installPollError.textContent, /upstream temporarily unavailable/);
  assert.equal(transient.elements.installPollRetry.hidden, false);
  assert.equal(transient.elements.installPollLogin.hidden, true);
  assert.equal(transient.timers.size, 0);
  await transient.elements.installPollRetry.listeners.click();
  assert.equal(transient.elements.installPollError.hidden, true);
  assert.equal(transient.elements.installPollRetry.hidden, true);
  assert.equal(transient.elements.log.textContent, 'latest output');
  assert.equal(transient.timers.size, 1);

  const receiptRecovery = makeContext([
    response(200, JSON.stringify({running: true, status_label: '安装中', returncode: null,
      error: '', log: 'output before completion'}), 'application/json'),
    response(503, JSON.stringify({ok: false, running: false, status: 'unknown',
      receipt_pending: true, error: 'terminal receipt temporarily unavailable', log: ''}), 'application/json'),
    response(200, JSON.stringify({ok: true, running: false, status: 'succeeded',
      status_label: '安装成功', returncode: 0, error: '', log: 'complete output'}), 'application/json')
  ]);
  await tick();
  const receiptTimer = [...receiptRecovery.timers.values()][0];
  await receiptTimer.callback();
  assert.equal(receiptRecovery.elements.log.textContent, 'output before completion');
  assert.equal(receiptRecovery.elements.installPollRetry.hidden, false);
  assert.match(receiptRecovery.elements.installPollError.textContent, /terminal receipt temporarily unavailable/);
  assert.equal(receiptRecovery.timers.size, 0);
  await receiptRecovery.elements.installPollRetry.listeners.click();
  assert.equal(receiptRecovery.elements.log.textContent, 'complete output');
  assert.equal(receiptRecovery.elements.installStatus.textContent, '安装成功');
  assert.equal(receiptRecovery.elements.installPollError.hidden, true);
  assert.equal(receiptRecovery.timers.size, 0);

  for(const status of [401, 403]) {
    const message = status === 401 ? '登录已过期' : '无权读取安装日志';
    const auth = makeContext([
      response(200, JSON.stringify({running: true, status_label: '安装中', returncode: null,
        error: '', log: 'previously read output'}), 'application/json'),
      response(status, JSON.stringify({ok: false, msg: message}), 'application/json')
    ]);
    await tick();
    const authTimer = [...auth.timers.values()][0];
    await authTimer.callback();
    assert.equal(auth.elements.log.textContent, 'previously read output');
    assert.equal(auth.elements.installStatus.textContent, '安装中');
    assert.equal(auth.elements.installPollRetry.hidden, true);
    assert.equal(auth.elements.installPollLogin.hidden, false);
    assert.equal(auth.elements.installPollLogin.href,
      '/login?next=%2Fdeps%2Finstall-log%2Fdemo-id%3Fback%3D%252Fdeps');
    assert.equal(auth.timers.size, 0);
  }
  for(const body of ['null', '{}', '{"running":"false","log":"bad"}']) {
    const invalid = makeContext([
      response(200, JSON.stringify({running: true, status_label: '安装中', log: 'preserved'}), 'application/json'),
      response(200, body, 'application/json')
    ]);
    await tick();
    await [...invalid.timers.values()][0].callback();
    assert.equal(invalid.elements.log.textContent, 'preserved');
    assert.equal(invalid.elements.installStatus.textContent, '安装中');
    assert.equal(invalid.elements.installPollRetry.hidden, false);
    assert.equal(invalid.timers.size, 0);
  }
  {
    const stale = makeContext([
      response(200, JSON.stringify({running: true, status_label: '安装中', log: 'current'}), 'application/json')
    ]);
    await tick();
    let resolve;
    let requests = 0;
    stale.context.fetch = () => { requests++; return new Promise(done => { resolve = done; }); };
    const pending = [...stale.timers.values()][0].callback();
    await stale.elements.installPollRetry.listeners.click();
    assert.equal(requests, 1);
    stale.elements.installPollError = {};
    stale.context.window.__FLS_ACTIVE_LOG_INTERVAL__ = null;
    stale.timers.clear();
    resolve(response(200, JSON.stringify({running: true, log: 'old page result'}), 'application/json'));
    await pending;
    assert.equal(stale.elements.log.textContent, 'current');
    assert.equal(stale.timers.size, 0);
  }
  console.log('dependency install log recovery passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""],
                input=json.dumps({"script": script, "helpers": helpers}),
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
