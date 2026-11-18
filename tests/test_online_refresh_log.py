import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_ui_route_components import ROOT, TOKEN, isolated_app


class OnlineRefreshLogTests(unittest.TestCase):
    def _state(self, base_dir, *, refresh_id="a" * 32, request_id="b" * 32, log_file=None):
        from fls_manager.online_scripts.constants import update_online_refresh_state

        path = Path(log_file or (base_dir / "log" / f"online-script-source-refresh-{refresh_id}.log"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("refresh started\nrefresh failed: upstream timeout\n", encoding="utf-8")
        update_online_refresh_state(
            id=refresh_id,
            request_id=request_id,
            proxy_id="",
            running=False,
            status="失败",
            message="",
            error="upstream timeout",
            cache_published=False,
            task_link_failures=[],
            updated_at="now",
            log_file=str(path),
        )
        return request_id

    def test_refresh_log_api_and_page_bind_request_and_bound_file(self):
        with isolated_app() as (app, base_dir):
            request_id = self._state(base_dir)
            client = app.test_client()
            response = client.get(
                f"/api/online-scripts/refresh-log/{request_id}?lines=20",
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json["ok"])
            self.assertEqual(response.json["request_id"], request_id)
            self.assertTrue(response.json["terminal"])
            self.assertIn("refresh failed", response.json["log"])
            page = client.get(f"/online-scripts/refresh-log/{request_id}", headers={"X-Token": TOKEN})
            self.assertEqual(page.status_code, 200)
            self.assertIn("refresh failed", page.get_data(as_text=True))
            self.assertIn(f"/api/online-scripts/refresh-log/{request_id}", page.get_data(as_text=True))
            status_page = client.get("/online-scripts", headers={"X-Token": TOKEN})
            self.assertEqual(status_page.status_code, 200)
            status_html = status_page.get_data(as_text=True)
            self.assertIn(f'id="onlineRefreshLogLink"', status_html)
            self.assertIn(
                f'id="onlineRefreshLogLink" class="btn btn-blue" href="/online-scripts/refresh-log/{request_id}"',
                status_html,
            )
            self.assertNotIn(
                f'id="onlineRefreshLogLink" class="btn btn-blue" href="/online-scripts/refresh-log/{request_id}" hidden',
                status_html,
            )
            invalid_lines = client.get(
                f"/api/online-scripts/refresh-log/{request_id}?lines=bad",
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(invalid_lines.status_code, 400)

    def test_refresh_log_rejects_mismatch_symlink_and_outside_path_without_reading(self):
        with isolated_app() as (app, base_dir):
            request_id = self._state(base_dir)
            client = app.test_client()
            missing = client.get(
                "/api/online-scripts/refresh-log/" + "c" * 32,
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(missing.status_code, 404)

            from fls_manager.online_scripts.constants import update_online_refresh_state

            outside = base_dir / "outside.log"
            outside.write_text("outside", encoding="utf-8")
            update_online_refresh_state(log_file=str(outside))
            rejected = client.get(
                f"/api/online-scripts/refresh-log/{request_id}",
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(rejected.status_code, 500)
            self.assertIn("受控日志目录", rejected.json["error"])

            expected = base_dir / "log" / f"online-script-source-refresh-{'a' * 32}.log"
            link = base_dir / "log" / "online-script-source-refresh-link.log"
            try:
                link.symlink_to(expected)
            except (OSError, NotImplementedError):
                self.skipTest("symlink is unavailable")
            update_online_refresh_state(log_file=str(link))
            symlink_response = client.get(
                f"/api/online-scripts/refresh-log/{request_id}",
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(symlink_response.status_code, 500)
            self.assertIn("日志路径", symlink_response.json["error"])

    def test_refresh_log_state_marks_running_until_terminal(self):
        with isolated_app() as (app, base_dir):
            request_id = self._state(base_dir)
            from fls_manager.online_scripts.constants import update_online_refresh_state

            update_online_refresh_state(running=True, status="运行中", message="still working")
            client = app.test_client()
            running = client.get(
                f"/api/online-scripts/refresh-log/{request_id}", headers={"X-Token": TOKEN},
            )
            self.assertEqual(running.status_code, 200)
            self.assertFalse(running.json["terminal"])
            self.assertTrue(running.json["running"])

            update_online_refresh_state(running=False, status="已完成", message="done")
            terminal = client.get(
                f"/api/online-scripts/refresh-log/{request_id}", headers={"X-Token": TOKEN},
            )
            self.assertEqual(terminal.status_code, 200)
            self.assertTrue(terminal.json["terminal"])
            self.assertFalse(terminal.json["running"])

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise refresh log polling")
    def test_refresh_log_page_polls_running_state_and_stops_at_terminal(self):
        with isolated_app() as (app, base_dir):
            request_id = self._state(base_dir)
            from fls_manager.online_scripts.constants import update_online_refresh_state

            update_online_refresh_state(running=True, status="运行中", message="still working")
            html = app.test_client().get(
                f"/online-scripts/refresh-log/{request_id}", headers={"X-Token": TOKEN},
            ).get_data(as_text=True)
            script = next(script for script in re.findall(r"<script>(.*?)</script>", html, re.S) if "schedulePolling" in script)
            static = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
            reader = static[static.index("async function flsReadJsonResponse("):static.index("async function flsReadTextResponse(")]
            result = subprocess.run(
                ["node", "-e", r'''
const vm = require('node:vm');
const assert = require('node:assert/strict');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
(async()=>{
 const elements={refreshLogError:{hidden:true,textContent:''},refreshLogLogin:{hidden:true,href:''},refreshLogRetry:{disabled:false},refreshLogText:{textContent:'old'},refreshLogStatus:{textContent:'运行中'},refreshLogUpdatedAt:{textContent:'old'}};
 const timers=[]; let pagehide=null; let calls=0;
 const responses=[
   {ok:true,status:200,body:{ok:true,log:'first line',status:'运行中',updated_at:'one',running:true,terminal:false}},
   {ok:true,status:200,body:{ok:true,log:'final line',status:'已完成',updated_at:'two',running:false,terminal:true}},
 ];
 const fakeSetTimeout=fn=>{timers.push(fn);return fn;};
 const context={document:{getElementById:id=>elements[id]},window:{addEventListener:(name,fn)=>{if(name==='pagehide') pagehide=fn;}},location:{pathname:'/online-scripts/refresh-log/'+('b'.repeat(32)),search:''},encodeURIComponent,
   fetch:async()=>{const item=responses[calls++];return {ok:item.ok,status:item.status,headers:{get:()=> 'application/json'},text:async()=>JSON.stringify(item.body)};},
   setTimeout:fakeSetTimeout,clearTimeout:fn=>{const index=timers.indexOf(fn);if(index>=0)timers.splice(index,1);},
   flsResponseErrorDiagnostic:(_r,b)=>({payload:JSON.parse(b),message:'backend diagnostic'})};
 elements.refreshLogRetry.addEventListener=()=>{};
 vm.createContext(context);vm.runInContext(input.reader+input.script,context);
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(calls,1); assert.equal(timers.length,1); assert.equal(elements.refreshLogText.textContent,'first line');
 const timer=timers.shift(); timer(); await new Promise(resolve=>setImmediate(resolve));
 assert.equal(calls,2); assert.equal(timers.length,0); assert.equal(elements.refreshLogText.textContent,'final line');
 assert.equal(elements.refreshLogStatus.textContent,'已完成'); assert.equal(typeof pagehide,'function');
 console.log('refresh log polling passed');
})().catch(error=>{console.error(error);process.exitCode=1});
'''], input=json.dumps({"script": script, "reader": reader}), text=True, capture_output=True, timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise refresh log recovery")
    def test_refresh_log_page_restores_retry_and_login_actions_on_http_errors(self):
        with isolated_app() as (app, base_dir):
            request_id = self._state(base_dir)
            html = app.test_client().get(
                f"/online-scripts/refresh-log/{request_id}", headers={"X-Token": TOKEN},
            ).get_data(as_text=True)
            script = next(script for script in re.findall(r"<script>(.*?)</script>", html, re.S) if "loadRefreshLog" in script)
            static = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
            reader = static[static.index("async function flsReadJsonResponse("):static.index("async function flsReadTextResponse(")]
            result = subprocess.run(
                ["node", "-e", r'''
const vm = require('node:vm');
const assert = require('node:assert/strict');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
(async()=>{
 for(const status of [401,403,404,500,502]){
  const elements={refreshLogError:{hidden:true,textContent:''},refreshLogLogin:{hidden:true,href:''},refreshLogRetry:{disabled:false},refreshLogText:{textContent:'old'},refreshLogStatus:{textContent:'失败'},refreshLogUpdatedAt:{textContent:'old'}};
  let click=null;
  const context={document:{getElementById:id=>elements[id]},window:{addEventListener:()=>{}},location:{pathname:'/online-scripts/refresh-log/'+('b'.repeat(32)),search:''},encodeURIComponent,
    fetch:async()=>({ok:false,status,headers:{get:()=> 'application/json'},text:async()=>JSON.stringify({error:'backend diagnostic'})}),
    setTimeout,flsResponseErrorDiagnostic:(_r,b)=>({payload:JSON.parse(b),message:'backend diagnostic'})};
  elements.refreshLogRetry.addEventListener=(name,fn)=>{if(name==='click') click=fn;};
  vm.createContext(context);vm.runInContext(input.reader+input.script,context);await new Promise(resolve=>setImmediate(resolve));
  await click();
  assert.equal(elements.refreshLogRetry.disabled,false);
  assert.match(elements.refreshLogError.textContent,/刷新日志读取失败/);
  assert.equal(elements.refreshLogLogin.hidden,!(status===401||status===403));
 }
 console.log('refresh log error recovery passed');
})().catch(error=>{console.error(error);process.exitCode=1});
'''], input=json.dumps({"script": script, "reader": reader}), text=True, capture_output=True, timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
