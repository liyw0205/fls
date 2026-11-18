"""Real HTTP/Chromium recovery for online and dependency install logs."""
import os
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading

from tools.browser_regression import TOKEN, find_chromium, find_playwright_core, seed_browser_fixture


def main():
    playwright, chromium = find_playwright_core(), find_chromium()
    if not playwright or not chromium:
        raise SystemExit("Set FLS_PLAYWRIGHT_CORE and FLS_CHROMIUM")
    with tempfile.TemporaryDirectory(prefix="fls-install-log-browser-") as temp:
        seed_browser_fixture(temp)
        os.environ.update(FLS_BASE_DIR=temp, FLS_TOKEN=TOKEN, FLS_SECRET_KEY="install-log-browser")
        from flask import Response, jsonify, request
        from werkzeug.serving import make_server
        from fls_manager.app import create_app
        import fls_manager.app as app_module
        from fls_manager import scheduler
        from fls_manager.install_records import INSTALL_JOBS_FILE, persist_install_job
        from fls_manager.online_scripts import install, source
        from fls_manager.online_scripts.constants import (
            ONLINE_INSTALL_JOBS_FILE, ONLINE_INSTALL_RETRYING, ONLINE_INSTALL_RUNNING,
        )
        from fls_manager.state import DEPS_RUNNING

        base_dir = Path(temp)
        release = base_dir / "release"
        command = (
            "import pathlib,time,sys; "
            f"p=pathlib.Path({str(release)!r}); "
            "print('live installer output',flush=True); "
            "deadline=time.monotonic()+90; "
            "exec('while not p.exists() and time.monotonic()<deadline: time.sleep(0.05)'); "
            "sys.exit(int(p.read_text()) if p.exists() else 99)"
        )
        item = {
            "id": "browser-recovery", "name": "Install recovery", "type": "raw",
            "link": "https://fixture.invalid/demo.py", "link_name": "demo.py",
            "install": shlex.join([sys.executable, "-u", "-c", command]),
        }
        source.save_online_script_cache([item])
        downloads = []

        def download(**kwargs):
            downloads.append(kwargs["install_id"])
            target = base_dir / "scripts" / "demo.py"
            target.write_text("print('local fixture')\n", encoding="utf-8")
            return target

        install.download_online_script_logged = download
        app = create_app()
        retry_parent_id = "browser-retry-parent"
        retry_child_id = "browser-retry-child"
        retry_parent_log = base_dir / "log" / f"deps-install-demo-{retry_parent_id}.log"
        retry_child_log = base_dir / "log" / f"deps-install-demo-{retry_child_id}.log"

        def seed_retry_chain():
            INSTALL_JOBS_FILE.write_text("{}", encoding="utf-8")
            retry_parent_log.parent.mkdir(parents=True, exist_ok=True)
            retry_parent_log.write_text("parent attempt failed\n", encoding="utf-8")
            retry_child_log.write_text("retry attempt failed\n", encoding="utf-8")
            persist_install_job({
                "id": retry_parent_id, "request_id": retry_parent_id, "kind": "dependency",
                "package": "demo", "log_file": str(retry_parent_log), "running": False,
                "finished": True, "status": "failed", "returncode": 1,
                "error": "parent attempt failed", "start_time": 1,
                "updated_at": "2026-11-18 20:10:00",
            })
            persist_install_job({
                "id": retry_child_id, "request_id": retry_child_id, "kind": "dependency",
                "retry_of": retry_parent_id, "package": "demo", "log_file": str(retry_child_log),
                "running": False, "finished": True, "status": "failed", "returncode": 2,
                "error": "retry attempt failed", "start_time": 2,
                "updated_at": "2026-11-18 20:11:00",
            })

        def seed_online_history():
            jobs = {}

            def online_record(install_id, started, updated, retry_of="", retried_by=""):
                log_file = base_dir / "log" / f"{install_id}.log"
                log_file.parent.mkdir(parents=True, exist_ok=True)
                log_file.write_text(f"log for {install_id}\n", encoding="utf-8")
                return {
                    "id": install_id, "script_id": "browser-recovery", "script_name": "Install recovery",
                    "log_file": str(log_file), "proxy_id": "", "import_task": False,
                    "enable_task": False, "selected_task_indexes": [], "selected_tasks": [],
                    "force": False, "retry_of": retry_of, "retried_by": retried_by,
                    "running": False, "status": "失败", "phase": "failed",
                    "start_time": started, "returncode": 1, "error": "fixture failure",
                    "scheduler_reload_pending": False, "scheduler_reload_error": "",
                    "updated_at": updated,
                }

            chain = [
                online_record("online-old-parent", 1, "2026-11-01 00:00:00", retried_by="online-old-child"),
                online_record("online-old-child", 2, "2026-11-01 00:01:00", retry_of="online-old-parent"),
                online_record("online-latest-parent", 200, "2026-11-18 20:00:00", retried_by="online-latest-middle"),
                online_record("online-latest-middle", 201, "2026-11-18 20:01:00", retry_of="online-latest-parent", retried_by="online-latest-final"),
                online_record("online-latest-final", 202, "2026-11-18 20:02:00", retry_of="online-latest-middle"),
            ]
            jobs.update((item["id"], item) for item in chain)
            for index in range(95):
                item = online_record(f"online-filler-{index:03d}", 10 + index, "2026-11-10 00:00:00")
                jobs[item["id"]] = item
            ONLINE_INSTALL_JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
            ONLINE_INSTALL_JOBS_FILE.write_text(json.dumps(jobs, ensure_ascii=False), encoding="utf-8")
            ONLINE_INSTALL_RUNNING.clear()
            ONLINE_INSTALL_RETRYING.clear()
            (base_dir / "scripts" / "demo.py").unlink(missing_ok=True)
        app_module.dispatch_frontend_open_event = lambda: None
        fault = {"status": 0}
        dependency_processes = []

        class CancelProcess:
            def __init__(self, process, fail_first_terminate=False):
                self.process = process
                self.fail_first_terminate = fail_first_terminate

            def poll(self):
                return self.process.poll()

            def terminate(self):
                if self.fail_first_terminate:
                    self.fail_first_terminate = False
                    raise OSError("fixture terminate denied")
                return self.process.terminate()

            def wait(self, timeout=None):
                return self.process.wait(timeout=timeout)

            def kill(self):
                return self.process.kill()

        @app.before_request
        def log_failure():
            if request.path.startswith(("/api/deps/install-log/", "/api/online-scripts/log/")):
                if fault["status"] == 502:
                    return Response("fixture upstream unavailable", status=502, content_type="text/plain")
                if fault["status"] == 200:
                    return Response("not JSON", content_type="text/html")

        @app.route("/api/fixture/install-logs", methods=["GET", "POST"])
        def fixture():
            if request.method == "POST":
                data = request.get_json()
                fault["status"] = int(data.get("status") or 0)
                if data.get("seed_retry_chain"):
                    seed_retry_chain()
                if data.get("seed_online_history"):
                    seed_online_history()
                if data.get("remove_online_target"):
                    (base_dir / "scripts" / "demo.py").unlink(missing_ok=True)
                if data.get("fill_capacity"):
                    for index in range(99):
                        install_id = f"browser-capacity-{index:03d}"
                        persist_install_job({
                            "id": install_id, "request_id": install_id, "kind": "dependency",
                            "package": "demo", "log_file": str(base_dir / "log" / f"{install_id}.log"),
                            "running": False, "status": "succeeded", "returncode": 0,
                            "error": "", "updated_at": "2026-11-18 22:00:00",
                        })
                    latest_id = "browser-capacity-latest"
                    latest_log = base_dir / "log" / f"deps-install-demo-{latest_id}.log"
                    latest_log.write_text("latest retained failure\n", encoding="utf-8")
                    persist_install_job({
                        "id": latest_id, "request_id": latest_id, "kind": "dependency",
                        "package": "demo", "log_file": str(latest_log), "running": False,
                        "status": "failed", "returncode": 7, "error": "latest install failed",
                        "updated_at": "2026-11-18 23:00:00",
                    })
                if data.get("reset"):
                    release.unlink(missing_ok=True)
                    (base_dir / "scripts" / "demo.py").unlink(missing_ok=True)
                    log_file = base_dir / "log" / "deps-install-demo-browser-deps.log"
                    fp = log_file.open("wb", buffering=0)
                    proc = subprocess.Popen([sys.executable, "-u", "-c", command], stdout=fp, stderr=subprocess.STDOUT)
                    dependency_processes.append(proc)
                    DEPS_RUNNING["browser-deps"] = {
                        "kind": "dependency", "package": "demo", "process": proc,
                        "log_file": str(log_file), "log_fp": fp, "finished": False,
                        "returncode": None,
                    }
                if data.get("reset_cancel"):
                    cancel_id = str(data.get("cancel_id") or "browser-cancel")
                    log_file = base_dir / "log" / f"deps-install-demo-{cancel_id}.log"
                    fp = log_file.open("wb", buffering=0)
                    proc = subprocess.Popen(
                        [sys.executable, "-u", "-c", "import time; print('live cancel output', flush=True); time.sleep(120)"],
                        stdout=fp, stderr=subprocess.STDOUT,
                    )
                    dependency_processes.append(proc)
                    cancel_process = CancelProcess(proc, data.get("fail_first_terminate", False))
                    info = {
                        "id": cancel_id, "request_id": cancel_id,
                        "kind": "dependency", "package": "demo",
                        "log_file": str(log_file), "log_fp": fp, "process": cancel_process,
                        "running": True, "finished": False, "status": "running",
                        "returncode": None, "error": "", "start_time": 1,
                    }
                    DEPS_RUNNING[cancel_id] = info
                    persist_install_job(info)
                if "release" in data:
                    release.write_text(str(data["release"]), encoding="ascii")
            return jsonify(
                downloads=len(downloads),
                online=[{"id": job_id, "running": info.get("running")} for job_id, info in ONLINE_INSTALL_RUNNING.items()],
                deps_running=bool(DEPS_RUNNING.get("browser-deps", {}).get("process") and
                                  DEPS_RUNNING["browser-deps"]["process"].poll() is None),
            )

        server = make_server("127.0.0.1", 0, app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            env = dict(os.environ, FLS_UI_BASE=f"http://127.0.0.1:{server.server_port}",
                       FLS_UI_TOKEN=TOKEN, FLS_PLAYWRIGHT_CORE=str(playwright), FLS_CHROMIUM=str(chromium))
            subprocess.run(["node", "-e", SCRIPT], env=env, check=True, timeout=240)
        finally:
            release.write_text("0", encoding="ascii")
            for proc in dependency_processes:
                if proc.poll() is None:
                    proc.terminate()
                proc.wait(timeout=5)
            for job_id in list(ONLINE_INSTALL_RUNNING):
                install.request_stop_online_install(job_id)
            for info in DEPS_RUNNING.values():
                if info.get("log_fp"):
                    info["log_fp"].close()
            server.shutdown()
            server.server_close()
            scheduler.scheduler.shutdown(wait=False)


SCRIPT = r'''
const assert=require('node:assert/strict');
const {chromium}=require(process.env.FLS_PLAYWRIGHT_CORE);
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.FLS_CHROMIUM,args:['--no-sandbox']});
 try{
  for(const viewport of [{width:390,height:844},{width:1280,height:800}]){
   const context=await browser.newContext({viewport}),page=await context.newPage();
   const errors=[];page.on('pageerror',e=>errors.push(e.message));page.on('dialog',d=>d.accept());
   const base=process.env.FLS_UI_BASE,headers={'X-Token':process.env.FLS_UI_TOKEN};
   const fixture=async data=>{const r=await page.request.post(base+'/api/fixture/install-logs',{headers,data});assert.equal(r.status(),200);return r.json();};
   const state=async()=>(await page.request.get(base+'/api/fixture/install-logs',{headers})).json();
   const login=async()=>{
    if(new URL(page.url()).pathname==='/login'){
     await page.locator('[name="token"]').fill(process.env.FLS_UI_TOKEN);
     await page.locator('button[type="submit"]').click();
     await page.waitForLoadState('domcontentloaded');
    }
   };
   await fixture({seed_retry_chain:true});await fixture({reset:true});
   await page.goto(base+'/login');await login();
   await page.goto(base+'/deps/install-history?kind=dependency');
   const retryParent=page.locator('tr').filter({hasText:'browser-retry-parent'});
   assert.equal((await retryParent.locator('td').nth(7).textContent()).trim(),'1');
   const retryLink=retryParent.getByRole('link',{name:/后续重试.*browser-retry-child/});
   await retryLink.waitFor();
   assert.match(await retryLink.getAttribute('href'),/browser-retry-child\?back=\/deps\/install-history$/);
   await retryLink.click();
   await page.waitForURL('**/deps/install-log/browser-retry-child?back=/deps/install-history');
   await page.waitForFunction(()=>document.querySelector('#installStatus').textContent==='安装失败');
   assert.ok((await page.locator('#log').textContent()).includes('retry attempt failed'));
   assert.ok(await page.locator('.install-retry-form [name="request_id"]').inputValue());
   await page.goto(base+'/deps/install-history?kind=dependency');
   const retryChild=page.locator('tr').filter({hasText:'browser-retry-child'});
   const parentLink=retryChild.getByRole('link',{name:/原安装.*browser-retry-parent/});
   await parentLink.click();
   await page.waitForURL('**/deps/install-log/browser-retry-parent?back=/deps/install-history');
   await page.waitForFunction(()=>document.querySelector('#installStatus').textContent==='安装失败');
   assert.equal(await page.locator('.install-retry-form').count(),0);
   await page.getByRole('link',{name:'查看已提交重试'}).click();
   await page.waitForURL('**/deps/install-log/browser-retry-child?back=/deps/install-history');
   await fixture({fill_capacity:true});
   await page.goto(base+'/deps/install-history?kind=dependency&limit=100');
   assert.equal(await page.locator('tr').filter({hasText:'browser-retry-parent'}).count(),0);
   assert.equal(await page.locator('tr').filter({hasText:'browser-retry-child'}).count(),0);
   const latest=page.locator('tr').filter({hasText:'browser-capacity-latest'});
   assert.equal((await latest.locator('td').nth(3).textContent()).trim(),'安装失败');
   assert.equal((await latest.locator('td').nth(7).textContent()).trim(),'7');
   await latest.getByRole('link',{name:'查看日志与恢复',exact:true}).click();
   await page.waitForURL('**/deps/install-log/browser-capacity-latest?back=/deps');
   await page.waitForFunction(()=>document.querySelector('#installStatus').textContent==='安装失败');
   assert.ok((await page.locator('#log').textContent()).includes('latest retained failure'));
   assert.ok(await page.locator('.install-retry-form [name="request_id"]').inputValue());
   const before=(await state()).downloads;
   const csrf=await page.evaluate(()=>flsGetCsrfToken());
   const installRequestId=require('node:crypto').randomBytes(16).toString('hex');
   const installForm={request_id:installRequestId};
   const start=await page.request.post(base+'/online-scripts/install/browser-recovery',{
     headers:{'X-CSRF-Token':csrf,'X-Requested-With':'XMLHttpRequest'},form:installForm,maxRedirects:0});
   assert.equal(start.status(),302);const onlinePath=start.headers().location;
   const replay=await page.request.post(base+'/online-scripts/install/browser-recovery',{
     headers:{'X-CSRF-Token':csrf,'X-Requested-With':'XMLHttpRequest'},form:installForm,maxRedirects:0});
   assert.equal(replay.status(),302);assert.equal(replay.headers().location,onlinePath);
   const paths=[onlinePath,'/deps/install-log/browser-deps?back=%2Fonline-scripts'];
   for(const path of paths){
    await page.goto(base+path);
    await page.waitForFunction(()=>document.querySelector('#log').textContent.split('\n').includes('live installer output'));
    const original=await page.locator('#log').textContent(),status=await page.locator('#installStatus').textContent();
    for(const code of [502,200]){
     await fixture({status:code});
     const diagnostic=code===502?'fixture upstream unavailable':'响应解析失败';
     await page.waitForFunction(text=>document.querySelector('#installPollError').textContent.includes(text),diagnostic);
     assert.equal(await page.locator('#log').textContent(),original);
     assert.equal(await page.locator('#installStatus').textContent(),status);
     assert.equal((await state()).deps_running,true);
     await fixture({});await page.locator('#installPollRetry').click();
     await page.waitForFunction(()=>document.querySelector('#installPollError').hidden);
    }
    await context.clearCookies();
    await page.waitForFunction(()=>!document.querySelector('#installPollLogin').hidden);
    assert.equal(await page.evaluate(()=>window.__FLS_ACTIVE_LOG_INTERVAL__),null);
    assert.equal(await page.locator('#log').textContent(),original);
    await page.locator('#installPollLogin').click();await page.waitForLoadState('domcontentloaded');await login();
    assert.equal(new URL(page.url()).pathname,new URL(base+path).pathname);
    await page.waitForFunction(()=>document.querySelector('#log').textContent.includes('live installer output'));
    assert.equal(await page.locator('#installPollLogin').isVisible(),false);
    const apiPattern=path.startsWith('/online-scripts/')?'**/api/online-scripts/log/*':'**/api/deps/install-log/*';
    await page.route(apiPattern,async route=>{
     const response=await route.fetch({url:route.request().url()+'&token=forbidden'});
     assert.equal(response.status(),403);await route.fulfill({response});
    },{times:1});
    await page.waitForFunction(()=>!document.querySelector('#installPollLogin').hidden);
    assert.equal(await page.evaluate(()=>window.__FLS_ACTIVE_LOG_INTERVAL__),null);
    if(path.startsWith('/online-scripts/')){
     await page.locator('#installPollRetry').click();
     await page.waitForFunction(()=>document.querySelector('#installPollError').hidden);
     assert.equal(await page.locator('#installStopAction').isVisible(),true);
     assert.ok(await page.evaluate(()=>window.__FLS_ACTIVE_LOG_INTERVAL__));
    }else{
     await fixture({});
     await page.reload();
     assert.equal(new URL(page.url()).pathname,new URL(base+path).pathname);
    }
    await page.waitForFunction(()=>document.querySelector('#log').textContent.includes('live installer output'));
    await page.evaluate(path=>{const a=document.createElement('a');a.id='fixture-revisit';a.href=path;a.textContent='revisit';document.querySelector('.content').append(a);},path);
    await page.locator('#fixture-revisit').click();
    await page.waitForFunction(()=>!document.querySelector('#fixture-revisit'));
    await page.waitForFunction(()=>document.querySelector('#log').textContent.includes('live installer output'));
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
   }
   assert.equal((await state()).downloads,before+1);
   await fixture({release:17});
   await page.waitForFunction(()=>document.querySelector('#installStatus').textContent==='安装失败');
   await page.goto(base+onlinePath);
   await page.waitForFunction(()=>document.querySelector('#installStatus').textContent==='失败');
   await page.locator('#installRecoveryAction').getByRole('button',{name:'重试安装',exact:true}).waitFor();
   assert.ok(await page.locator('#installRecoveryAction [name="csrf_token"]').inputValue());
   assert.equal(await page.evaluate(()=>window.__FLS_ACTIVE_LOG_INTERVAL__),null);
   await page.screenshot({path:'/tmp/fls-install-log-recovery-'+viewport.width+'.png',fullPage:true});
   const failed=await state();assert.equal(failed.deps_running,false);assert.equal(failed.online.some(j=>j.running),false);
   await page.locator('#installRecoveryAction').getByRole('button',{name:'重试安装',exact:true}).click();
   await page.getByRole('button',{name:'确认覆盖并继续',exact:true}).waitFor();
   await fixture({release:0});
   await page.getByRole('button',{name:'确认覆盖并继续',exact:true}).click();
   await page.waitForFunction(()=>document.querySelector('#installStatus')?.textContent==='已完成');
   assert.equal((await state()).downloads,before+2);
   assert.equal(await page.evaluate(()=>window.__FLS_ACTIVE_LOG_INTERVAL__),null);
   const cancelId='browser-cancel-'+viewport.width;
   await fixture({reset_cancel:true,cancel_id:cancelId});
   await page.goto(base+'/deps/install-log/'+cancelId+'?back=%2Fdeps');
   await page.waitForFunction(()=>document.querySelector('#log').textContent.includes('live cancel output'));
   const cancelForm=page.locator('.install-cancel-form');
   await cancelForm.getByRole('button',{name:'取消安装'}).waitFor();
   const cancelRequestId=await cancelForm.locator('[name="request_id"]').inputValue();
   let cancelPosts=0;
   await page.route('**/api/deps/install-cancel/'+cancelId,async route=>{
    if(route.request().method()==='POST'){
     cancelPosts++;
     const committed=await route.fetch();
     assert.equal(committed.status(),200);
     await route.fulfill({status:502,contentType:'text/plain',body:'fixture response lost after commit'});
    }else await route.continue();
   });
   await cancelForm.getByRole('button',{name:'取消安装'}).click();
   await page.waitForFunction(()=>document.querySelector('#installCancelError').textContent.includes('fixture response lost after commit'));
   assert.equal(cancelPosts,1);
   await page.reload();
   assert.equal(await page.locator('.install-cancel-form').getAttribute('data-query-on-load'),'true');
   assert.equal(await page.locator('.install-cancel-form [name="request_id"]').inputValue(),cancelRequestId);
   await page.waitForFunction(()=>document.querySelector('#installCancelError').textContent.includes('已取消'));
   assert.equal(await page.locator('.install-cancel-form button[type="submit"]').isHidden(),true);
   assert.equal(await page.locator('#installCancelAgain').isHidden(),true);
   assert.equal(cancelPosts,1);
   assert.equal((await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)),true);
   await page.screenshot({path:'/tmp/fls-install-cancel-recovery-'+viewport.width+'.png',fullPage:true});
   const failedCancelId='browser-cancel-failed-'+viewport.width;
   await fixture({reset_cancel:true,cancel_id:failedCancelId,fail_first_terminate:true});
   await page.goto(base+'/deps/install-log/'+failedCancelId+'?back=%2Fdeps');
   await page.waitForFunction(()=>document.querySelector('#log').textContent.includes('live cancel output'));
   let failedCancelPosts=0;
   await page.route('**/api/deps/install-cancel/'+failedCancelId,async route=>{
    if(route.request().method()==='POST') failedCancelPosts++;
    await route.continue();
   });
   const failedCancelForm=page.locator('.install-cancel-form');
   await failedCancelForm.getByRole('button',{name:'取消安装'}).click();
   await page.waitForFunction(()=>document.querySelector('#installCancelError').textContent.includes('fixture terminate denied'));
   assert.equal(failedCancelPosts,1);
   await page.locator('#installCancelQuery').click();
   await page.waitForFunction(()=>!document.querySelector('#installCancelAgain').hidden);
   const failedRequestId=await failedCancelForm.locator('[name="request_id"]').inputValue();
   await page.locator('#installCancelAgain').click();
   await page.waitForFunction(()=>document.querySelector('#installStatus').textContent==='已取消');
   assert.notEqual(await failedCancelForm.locator('[name="request_id"]').inputValue(),failedRequestId);
   assert.equal(failedCancelPosts,2);
   assert.equal(await page.locator('#installCancelAgain').isHidden(),true);
   await page.goto(base+'/deps/install-history?kind=dependency');
   const historyRow=page.locator('tr').filter({hasText:failedCancelId});
   await historyRow.waitFor();
   assert.equal((await historyRow.locator('td').nth(3).textContent()).trim(),'已取消');
   assert.equal((await historyRow.locator('td').nth(4).textContent()).trim(),'已取消');
   const historyRecovery=historyRow.getByRole('link',{name:'查看日志与恢复',exact:true});
   assert.match(await historyRecovery.getAttribute('href'),new RegExp('/deps/install-log/'+failedCancelId+'\\?back=/deps$'));
   await historyRecovery.click();
   await page.waitForURL('**/deps/install-log/'+failedCancelId+'?back=/deps');
   await page.locator('.install-retry-form button[type="submit"]').waitFor();
   assert.ok(await page.locator('.install-retry-form [name="request_id"]').inputValue());
   assert.equal((await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)),true);

   await fixture({seed_online_history:true});
   await page.goto(base+'/online-scripts');
   await page.locator('form[action="/online-scripts/install-retry/online-latest-final"]').waitFor();
   assert.ok((await page.locator('.content').textContent()).includes('上次安装状态：失败'));
   const onlineDownloadsBefore=(await state()).downloads;
   const capacityRequestId=require('node:crypto').randomBytes(16).toString('hex');
   const onlineCsrf=await page.evaluate(()=>flsGetCsrfToken());
   const capacityStart=await page.request.post(base+'/api/online-scripts/install/browser-recovery',{
    headers:{'X-CSRF-Token':onlineCsrf},data:{request_id:capacityRequestId}});
   assert.equal(capacityStart.status(),200,await capacityStart.text());
   const capacityResult=await capacityStart.json();
   assert.equal(capacityResult.ok,true);
   await page.waitForFunction(async id=>{
    const response=await fetch('/api/online-scripts/log/'+id,{cache:'no-store'});
    if(!response.ok)return false;const job=await response.json();return job.running===false;
   },capacityResult.install_id);
   const onlineHistoryResponse=await page.request.get(base+'/api/online-scripts/install-history?limit=100');
   assert.equal(onlineHistoryResponse.status(),200);
   const onlineHistory=await onlineHistoryResponse.json();
   const onlineIds=onlineHistory.items.map(item=>item.id);
   assert.equal(onlineIds.includes('online-old-parent'),false);
   assert.equal(onlineIds.includes('online-old-child'),false);
   for(const id of ['online-latest-parent','online-latest-middle','online-latest-final',capacityResult.install_id])
    assert.ok(onlineIds.includes(id),id+' should remain in the trimmed history');
   const onlineById=Object.fromEntries(onlineHistory.items.map(item=>[item.id,item]));
   assert.equal(onlineById['online-latest-middle'].retry_parent_id,'online-latest-parent');
   assert.equal(onlineById['online-latest-middle'].retry_child_id,'online-latest-final');
   assert.equal(onlineById['online-latest-final'].retry_relation_error,'');

   await page.goto(base+'/online-scripts/install-history');
   await page.locator('#install-history-script').fill('browser-recovery');
   await page.locator('#install-history-status').selectOption('失败');
   await page.locator('#install-history-limit').selectOption('1');
   await page.getByRole('button',{name:'筛选历史'}).click();
   await page.waitForFunction(()=>document.body.textContent.includes('当前显示 1–1 / '));
   let historyUrl=new URL(page.url());
   assert.equal(historyUrl.searchParams.get('script_id'),'browser-recovery');
   assert.equal(historyUrl.searchParams.get('status'),'失败');
   assert.equal(historyUrl.searchParams.get('limit'),'1');
   const firstFilteredRow=page.locator('tbody tr').first();
   assert.ok((await firstFilteredRow.textContent()).includes('online-latest-final'));
   await page.getByRole('link',{name:'下一页'}).click();
   await page.waitForFunction(()=>new URL(location.href).searchParams.get('offset')==='1');
   assert.ok((await page.locator('tbody tr').first().textContent()).includes('online-latest-middle'));
   const filteredSecondPage = page.url();
   await page.reload();
   await context.clearCookies();
   await page.reload();
   await page.waitForURL(url=>url.pathname==='/login');
   await login();
   await page.waitForURL(filteredSecondPage);
   assert.equal(await page.locator('#install-history-status').inputValue(),'失败');
   await page.getByRole('link',{name:'上一页'}).click();
   await page.waitForFunction(()=>!new URL(location.href).searchParams.has('offset'));
   await page.locator('tbody tr').first().getByRole('link',{name:'查看安装日志'}).click();
   await page.waitForURL(url=>url.pathname==='/online-scripts/log/online-latest-final'&&url.searchParams.has('back'));
   let logUrl=new URL(page.url());
   assert.equal(decodeURIComponent(logUrl.searchParams.get('back')),'/online-scripts/install-history?script_id=browser-recovery&status=失败&limit=1');
   await page.getByRole('link',{name:'查看原安装'}).click();
   await page.waitForURL(url=>url.pathname==='/online-scripts/log/online-latest-middle'&&url.searchParams.has('back'));
   logUrl=new URL(page.url());
   assert.equal(decodeURIComponent(logUrl.searchParams.get('back')),'/online-scripts/install-history?script_id=browser-recovery&status=失败&limit=1');
   await page.getByRole('link',{name:'返回',exact:true}).click();
   await page.waitForURL(url=>url.pathname==='/online-scripts/install-history'&&url.searchParams.get('limit')==='1');
   historyUrl=new URL(page.url());
   assert.equal(historyUrl.searchParams.get('script_id'),'browser-recovery');
   assert.equal(historyUrl.searchParams.get('status'),'失败');
   assert.equal(historyUrl.searchParams.get('limit'),'1');

   await page.goto(base+'/online-scripts/install-history');
   const latestParentRow=page.locator('tr').filter({hasText:'online-latest-parent'});
   await latestParentRow.getByRole('link',{name:'后续重试'}).click();
   await page.waitForURL('**/online-scripts/log/online-latest-middle?back=/online-scripts/install-history');
   await page.waitForFunction(()=>document.querySelector('#log').textContent.includes('log for online-latest-middle'));
   await page.getByRole('link',{name:'查看后续重试'}).click();
   await page.waitForURL('**/online-scripts/log/online-latest-final?back=/online-scripts/install-history');
   await page.waitForFunction(()=>document.querySelector('#log').textContent.includes('log for online-latest-final'));
   await page.getByRole('link',{name:'查看原安装'}).click();
   await page.waitForURL('**/online-scripts/log/online-latest-middle?back=/online-scripts/install-history');
   await fixture({remove_online_target:true});
   await page.goto(historyUrl.href);
   const latestFinalRow=page.locator('tr').filter({hasText:'online-latest-final'});
   await latestFinalRow.getByRole('link',{name:'查看安装日志'}).click();
   await page.waitForURL(url=>url.pathname==='/online-scripts/log/online-latest-final');
   await page.locator('#installRecoveryAction [name="csrf_token"]').waitFor({state:'attached'});
   await page.locator('#installRecoveryAction').getByRole('button',{name:'重试安装',exact:true}).click();
   await page.waitForURL(url=>/^\/online-scripts\/log\/[a-f0-9]{32}$/.test(url.pathname));
   const recoveredInstallId=new URL(page.url()).pathname.split('/').pop();
   assert.equal(decodeURIComponent(new URL(page.url()).searchParams.get('back')),
    '/online-scripts/install-history?script_id=browser-recovery&status=失败&limit=1');
   assert.notEqual(recoveredInstallId,'online-latest-final');
   await page.waitForFunction(()=>document.querySelector('#installStatus').textContent==='已完成');
   await page.goto(base+'/online-scripts/install-history');
   const retriedFinalRow=page.locator('tr').filter({hasText:'online-latest-final'});
   const newRetryLink=retriedFinalRow.getByRole('link',{name:'后续重试'});
   assert.match(await newRetryLink.getAttribute('href'),new RegExp('/online-scripts/log/'+recoveredInstallId+'\\?back=/online-scripts/install-history$'));
   await newRetryLink.click();
   await page.waitForURL('**/online-scripts/log/'+recoveredInstallId+'?back=/online-scripts/install-history');
   await page.getByRole('link',{name:'查看原安装'}).waitFor();
   assert.equal((await state()).downloads,onlineDownloadsBefore+2);
   await page.goto(historyUrl.href);
   assert.equal((await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)),true);
   await page.screenshot({path:'/tmp/fls-online-install-history-'+viewport.width+'.png',fullPage:true});
   assert.deepEqual(errors,[]);await context.close();console.log('install log recovery passed '+viewport.width);
  }
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
'''


if __name__ == "__main__":
    main()
