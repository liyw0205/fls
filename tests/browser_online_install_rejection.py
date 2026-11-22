"""Real HTTP/Chromium recovery for pre-accepted online install rejections."""
import json
import os
import tempfile
import threading
import subprocess
import time

from tools.browser_regression import TOKEN, find_chromium, find_playwright_core, seed_browser_fixture


def main():
    playwright, chromium = find_playwright_core(), find_chromium()
    if not playwright or not chromium:
        raise SystemExit("Set FLS_PLAYWRIGHT_CORE and FLS_CHROMIUM")
    with tempfile.TemporaryDirectory(prefix="fls-install-rejection-browser-") as temp:
        seed_browser_fixture(temp)
        os.environ.update(FLS_BASE_DIR=temp, FLS_TOKEN=TOKEN, FLS_SECRET_KEY="install-rejection-browser")
        from flask import jsonify, session
        from werkzeug.serving import make_server
        from fls_manager.app import create_app
        import fls_manager.app as app_module
        from fls_manager.online_scripts import source
        from fls_manager.online_scripts.constants import ONLINE_INSTALL_JOBS_FILE
        from fls_manager.routes.online_scripts import install as install_route

        item = {
            "id": "browser-install", "name": "浏览器安装拒绝恢复", "type": "raw",
            "link": "https://fixture.invalid/browser-install.py", "link_name": "browser-install.py",
            "task_cron": [{"name": "Keep selection", "command": "echo keep", "cron": ""},
                          {"name": "Excluded selection", "command": "echo exclude", "cron": ""}],
        }
        source.save_online_script_cache([item])
        starts = []

        def fake_install_start(*args, **kwargs):
            request_id = kwargs["request_id"]
            script = kwargs["item"]
            selected = list(kwargs.get("selected_task_indexes") or [])
            starts.append(request_id)
            install_route.persist_online_install({
                "id": request_id,
                "script_id": kwargs["script_id"],
                "script_name": script.get("name"),
                "log_file": os.path.join(temp, "logs", f"online-script-install-{request_id}.log"),
                "proxy_id": kwargs.get("proxy_id", ""),
                "import_task": bool(kwargs.get("import_task")),
                "enable_task": bool(kwargs.get("enable_task")),
                "selected_task_indexes": selected,
                "selected_tasks": [script["task_cron"][int(index) - 1] for index in selected],
                "force": bool(kwargs.get("force")),
                "retry_of": "",
                "retried_by": "",
                "running": False,
                "status": "已完成",
                "phase": "completed",
                "target_path": str(install_route.online_script_target(script)),
                "start_time": time.time(),
                "returncode": 0,
                "error": "",
                "scheduler_reload_pending": False,
                "scheduler_reload_error": "",
                "updated_at": "2099-01-01T00:00:00",
            })
            history = {}
            for index in range(100):
                install_id = f"legacy-{index:03d}"
                history[install_id] = {
                    "id": install_id,
                    "script_id": kwargs["script_id"],
                    "script_name": script.get("name"),
                    "log_file": os.path.join(temp, "logs", f"online-script-install-{install_id}.log"),
                    "proxy_id": "",
                    "import_task": False,
                    "enable_task": False,
                    "selected_task_indexes": [],
                    "selected_tasks": [],
                    "force": False,
                    "retry_of": "",
                    "retried_by": "",
                    "running": False,
                    "status": "已完成",
                    "phase": "completed",
                    "target_path": "",
                    "start_time": index + 1,
                    "returncode": 0,
                    "error": "",
                    "scheduler_reload_pending": False,
                    "scheduler_reload_error": "",
                    "updated_at": f"2026-11-20T00:00:{index:02d}",
                }
            history[request_id] = install_route.get_online_install_job(request_id)
            ONLINE_INSTALL_JOBS_FILE.write_text(json.dumps(history), encoding="utf-8")
            return {}

        real_start = install_route._start_install_job
        install_route._start_install_job = fake_install_start
        app = create_app()
        app_module.dispatch_frontend_open_event = lambda: None

        @app.route("/api/fixture/online-install/<request_id>")
        def inspect_install(request_id):
            return jsonify(
                ok=True,
                starts=starts,
                job=install_route.get_online_install_job(request_id),
            )

        @app.route("/api/fixture/expire-session", methods=["POST"])
        def expire_session():
            session["token_expire_at"] = 1
            return jsonify(ok=True)

        @app.route("/api/fixture/clear-online-script-cache", methods=["POST"])
        def clear_online_script_cache():
            source.save_online_script_cache([])
            return jsonify(ok=True)

        @app.route("/api/fixture/restore-online-script-cache", methods=["POST"])
        def restore_online_script_cache():
            source.save_online_script_cache([item])
            return jsonify(ok=True)

        server = make_server("127.0.0.1", 0, app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            env = dict(
                os.environ,
                FLS_UI_BASE=f"http://127.0.0.1:{server.server_port}",
                FLS_UI_TOKEN=TOKEN,
                FLS_PLAYWRIGHT_CORE=str(playwright),
                FLS_CHROMIUM=str(chromium),
            )
            subprocess.run(["node", "-e", SCRIPT], env=env, check=True, timeout=120)
        finally:
            install_route._start_install_job = real_start
            server.shutdown()
            server.server_close()


SCRIPT = r'''
const assert=require('node:assert/strict');
const {chromium}=require(process.env.FLS_PLAYWRIGHT_CORE);
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.FLS_CHROMIUM,args:['--no-sandbox']});
 try{
  const base=process.env.FLS_UI_BASE,headers={'X-Token':process.env.FLS_UI_TOKEN};
  for(const viewport of [{width:390,height:844},{width:1280,height:800}]){
   const context=await browser.newContext({viewport}),page=await context.newPage(),errors=[],historyQueries=[];
   page.on('pageerror',error=>errors.push(error.message));
   page.on('request',request=>{
     const url=new URL(request.url());
     if(url.pathname==='/api/online-scripts/install-history')historyQueries.push(url);
   });
   await page.goto(base+'/login');
   await page.locator('[name="token"]').fill(process.env.FLS_UI_TOKEN);
   await page.locator('button[type="submit"]').click();
   await page.goto(base+'/online-scripts/install-select/browser-install');
   await page.locator('.fls-install-task-checkbox[data-index="2"]').uncheck();
   await page.locator('[name="proxy_id"]').evaluate(select=>{
     select.add(new Option('Missing proxy fixture','missing-proxy'));
     select.value='missing-proxy';
   });
   const requestId=await page.locator('#onlineInstallSelectForm [name="request_id"]').inputValue();
   const initial=await page.request.get(base+'/api/fixture/online-install/'+requestId,{headers});
   assert.equal(initial.status(),200);
   const initialState=await initial.json();
   const post=page.waitForResponse(response=>response.url()===base+'/api/online-scripts/install/browser-install'
     &&response.request().method()==='POST');
   await page.getByRole('button',{name:'开始下载安装',exact:true}).click();
   const response=await post,body=await response.json();
   assert.equal(response.status(),400);
   assert.equal(body.accepted,false);
   assert.equal(body.request_id,requestId);
   assert.match(body.msg,/代理不存在或已停用/);
   assert.ok(response.request().headers()['x-csrf-token']);
   await page.waitForFunction(()=>{
     const form=document.getElementById('onlineInstallSelectForm');
     return sessionStorage.getItem('fls-online-install-request')===null
       &&form.__flsInstallRecoveryLocked===false
       &&form.__flsInstallSubmitting===false
       &&Array.from(form.querySelectorAll('button[type="submit"]')).every(button=>!button.disabled);
   });
   assert.equal(await page.locator('#onlineInstallSelectForm [name="request_id"]').inputValue(),requestId);
   assert.equal(await page.locator('[name="proxy_id"]').inputValue(),'missing-proxy');
   assert.equal(await page.locator('.fls-install-task-checkbox[data-index="2"]').isChecked(),false);
   assert.equal(await page.locator('#excludedTaskIndexes').inputValue(),'2');
   assert.match(await page.locator('#onlineInstallApiError').textContent(),/代理不存在或已停用/);
   const state=await page.request.get(base+'/api/fixture/online-install/'+requestId,{headers});
   assert.equal(state.status(),200);
   const persisted=await state.json();
   assert.equal(persisted.job,null);
   assert.deepEqual(persisted.starts,initialState.starts);

   await page.locator('[name="proxy_id"]').selectOption('');
   const acceptedPost=base+'/api/online-scripts/install/browser-install';
   await page.route(acceptedPost,async route=>{
     const accepted=await route.fetch();
     assert.equal(accepted.status(),200);
     await route.fulfill({status:502,contentType:'text/html',body:'gateway lost the install response'});
   });
   await page.getByRole('button',{name:'开始下载安装',exact:true}).click();
   await page.locator('#onlineInstallApiError').waitFor({state:'visible'});
   assert.match(await page.locator('#onlineInstallApiError').textContent(),/502/);
   assert.equal(await page.evaluate(()=>JSON.parse(sessionStorage.getItem('fls-online-install-request')).request_id),requestId);
   assert.equal(await page.evaluate(()=>document.getElementById('onlineInstallSelectForm').__flsInstallRecoveryLocked),true);
   assert.equal(await page.locator('.fls-install-task-checkbox[data-index="2"]').isChecked(),false);
   await page.unroute(acceptedPost);
   const expired=await page.request.post(base+'/api/fixture/expire-session',{headers});
   assert.equal(expired.status(),200);
   await page.reload();
   const loginUrl=new URL(page.url());
   assert.equal(loginUrl.pathname,'/login');
   assert.equal(loginUrl.searchParams.get('next'),'/online-scripts/install-select/browser-install');
   assert.equal(await page.evaluate(()=>JSON.parse(sessionStorage.getItem('fls-online-install-request')).request_id),requestId);
   await page.locator('[name="token"]').fill(process.env.FLS_UI_TOKEN);
   await page.locator('button[type="submit"]').click();
   await page.waitForURL(base+'/online-scripts/install-select/browser-install');
   await page.waitForFunction(()=>document.getElementById('onlineInstallRecoveryStatus').textContent.includes('原安装已明确结束'));
   assert.equal(await page.locator('#onlineInstallRecoveryLog').isVisible(),true);
   const historyQueryStart=historyQueries.length;
   await page.locator('#onlineInstallRecoveryCheckHistory').click();
   await page.waitForFunction(()=>document.getElementById('onlineInstallRecoveryClear').hidden===false);
   await page.waitForFunction(()=>document.getElementById('onlineInstallRecoveryStatus').textContent.includes('可以确认清理本地请求'));
   const historyPages=historyQueries.slice(historyQueryStart);
   assert.equal(historyPages.length,2);
   assert.equal(historyPages[0].searchParams.get('limit'),'100');
   assert.equal(historyPages[0].searchParams.get('offset'),null);
   assert.equal(historyPages[1].searchParams.get('offset'),'100');
   assert.match(historyPages[1].searchParams.get('snapshot'),/^[a-f0-9]{64}$/);
   assert.match(await page.locator('#onlineInstallRecoveryHistoryItems').textContent(),/101/);
   page.once('dialog',dialog=>dialog.accept());
   await page.locator('#onlineInstallRecoveryClear').click();
   await page.waitForFunction(()=>{
     const form=document.getElementById('onlineInstallSelectForm');
     return !!form&&sessionStorage.getItem('fls-online-install-request')===null
       &&form.__flsInstallRecoveryLocked===false;
   });
   const nextRequestId=await page.locator('#onlineInstallSelectForm [name="request_id"]').inputValue();
   assert.notEqual(nextRequestId,requestId);
   const cleared=await page.request.post(base+'/api/fixture/clear-online-script-cache',{headers});
   assert.equal(cleared.status(),200);
   const csrf=await page.evaluate(()=>flsGetCsrfToken());
   const replay=await page.request.post(acceptedPost,{headers:{...headers,'X-CSRF-Token':csrf},data:{
     request_id:requestId,proxy_id:'',import_task:'1',select_mode:'all',excluded_task_indexes:'2',
   }});
   assert.equal(replay.status(),200);
   assert.equal((await replay.json()).replayed,true);
   const restored=await page.request.get(base+'/api/fixture/online-install/'+requestId,{headers});
   const restoredState=await restored.json();
   assert.equal(restoredState.job.status,'已完成');
   assert.deepEqual(restoredState.starts,[...initialState.starts,requestId]);
   const restoredCache=await page.request.post(base+'/api/fixture/restore-online-script-cache',{headers});
   assert.equal(restoredCache.status(),200);
   assert.deepEqual(errors,[]);
   await page.screenshot({path:'/tmp/fls-install-rejection-'+viewport.width+'.png'});
   console.log('pre-acceptance rejection and accepted HTTP 502 recovery passed '+viewport.width);
   await context.close();
  }
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exit(1)});
'''


if __name__ == "__main__":
    main()
