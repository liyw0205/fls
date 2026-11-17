"""Real HTTP/Chromium recovery for online and dependency install logs."""
import os
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
        from fls_manager.online_scripts import install, source
        from fls_manager.online_scripts.constants import ONLINE_INSTALL_RUNNING
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
        app_module.dispatch_frontend_open_event = lambda: None
        fault = {"status": 0}
        dependency_processes = []

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
   await fixture({reset:true});await page.goto(base+'/login');await login();
   const before=(await state()).downloads;
   const csrf=await page.evaluate(()=>flsGetCsrfToken());
   const start=await page.request.post(base+'/online-scripts/install/browser-recovery',{
     headers:{'X-CSRF-Token':csrf,'X-Requested-With':'XMLHttpRequest'},form:{},maxRedirects:0});
   assert.equal(start.status(),302);const onlinePath=start.headers().location;
   const paths=[onlinePath,'/deps/install-log/browser-deps?back=%2Fonline-scripts'];
   for(const path of paths){
    await page.goto(base+path);
    await page.waitForFunction(()=>document.querySelector('#log').textContent.includes('live installer output'));
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
   assert.deepEqual(errors,[]);await context.close();console.log('install log recovery passed '+viewport.width);
  }
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
'''


if __name__ == "__main__":
    main()
