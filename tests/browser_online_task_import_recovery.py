"""Real HTTP/Chromium recovery for idempotent online task import."""
import os
import tempfile
import threading
import subprocess
import uuid

from tools.browser_regression import TOKEN, find_chromium, find_playwright_core, seed_browser_fixture


def main():
    playwright, chromium = find_playwright_core(), find_chromium()
    if not playwright or not chromium:
        raise SystemExit("Set FLS_PLAYWRIGHT_CORE and FLS_CHROMIUM")
    with tempfile.TemporaryDirectory(prefix="fls-task-import-browser-") as temp:
        seed_browser_fixture(temp)
        os.environ.update(FLS_BASE_DIR=temp, FLS_TOKEN=TOKEN, FLS_SECRET_KEY="task-import-browser")
        from flask import jsonify, request, session
        from werkzeug.serving import make_server
        from fls_manager.app import create_app
        import fls_manager.app as app_module
        from fls_manager import scheduler
        from fls_manager.models import load_tasks, save_tasks
        from fls_manager.online_scripts import constants, source, tasks
        from fls_manager.routes.online_scripts import install_select

        item = {
            "id": "browser-import", "name": "浏览器任务导入", "type": "raw",
            "link": "https://fixture.invalid/browser-import.py", "link_name": "browser-import.py",
            "task_cron": [{"name": "Browser task", "command": "echo browser", "cron": ""},
                          {"name": "Excluded task", "command": "echo excluded", "cron": ""}],
        }
        source.save_online_script_cache([item])
        fault = {"fail": True, "reloads": 0, "startup_failure": ""}

        def task_reload():
            fault["reloads"] += 1
            if fault["fail"]:
                raise RuntimeError("fixture scheduler unavailable")

        def recovery_reload():
            return None

        real_task_reload = tasks.reload_scheduler
        real_recovery_reload = install_select.reload_scheduler
        real_persist = install_select.persist_online_task_import_request

        def persist(record):
            mode = fault["startup_failure"]
            if mode and record["state"] == "running":
                fault["startup_failure"] = ""
                if mode == "after":
                    real_persist(record)
                raise OSError("browser import startup receipt unavailable")
            return real_persist(record)

        tasks.reload_scheduler = task_reload
        install_select.reload_scheduler = recovery_reload
        install_select.persist_online_task_import_request = persist
        app = create_app()
        app_module.dispatch_frontend_open_event = lambda: None

        @app.route("/api/fixture/task-import", methods=["POST"])
        def fixture():
            data = request.get_json(silent=True) or {}
            if data.get("expire"):
                session["token_expire_at"] = 1
                return jsonify(ok=True)
            if data.get("inspect"):
                return jsonify(ok=True, tasks=load_tasks(), reloads=fault["reloads"])
            source.save_online_script_cache([item])
            save_tasks([])
            constants.ONLINE_TASK_IMPORT_ACTIVE.clear()
            constants.mutate_json(constants.ONLINE_TASK_IMPORT_REQUESTS_FILE, {}, lambda _: {}, strict=True)
            fault.update(fail=data.get("fail", True), startup_failure=data.get("startup_failure", ""), reloads=0)
            request_id = uuid.uuid4().hex
            if data.get("prepared"):
                normalized = source.load_online_script_cache()[0]
                real_persist({
                    "request_id": request_id, "script_id": item["id"],
                    "expected_version": tasks.online_task_import_version(normalized),
                    "selected_task_indexes": ["1"], "enable_task": False, "state": "prepared",
                    "message": "original browser preparation diagnosis", "scheduler_reload_error": "", "updated_at": "old",
                })
            return jsonify(ok=True, request_id=request_id)

        server = make_server("127.0.0.1", 0, app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            env = dict(os.environ, FLS_UI_BASE=f"http://127.0.0.1:{server.server_port}",
                       FLS_UI_TOKEN=TOKEN, FLS_PLAYWRIGHT_CORE=str(playwright), FLS_CHROMIUM=str(chromium))
            subprocess.run(["node", "-e", SCRIPT], env=env, check=True, timeout=180)
        finally:
            tasks.reload_scheduler = real_task_reload
            install_select.reload_scheduler = real_recovery_reload
            install_select.persist_online_task_import_request = real_persist
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
   const errors=[];page.on('pageerror',e=>errors.push(e.message));
   const base=process.env.FLS_UI_BASE,headers={'X-Token':process.env.FLS_UI_TOKEN};
   const fixture=async data=>{const r=await page.request.post(base+'/api/fixture/task-import',{headers,data});assert.equal(r.status(),200);return r.json();};
   await fixture();
   await page.goto(base+'/login');
   await page.locator('[name="token"]').fill(process.env.FLS_UI_TOKEN);
   await page.locator('button[type="submit"]').click();
   const seeded=await fixture({prepared:true,fail:false});
   await page.evaluate(info=>sessionStorage.setItem('fls-online-install-request',JSON.stringify({
     request_id:info.request_id,script_id:'browser-import',action:'/online-scripts/import-tasks/browser-import',
     excluded_task_indexes:'2',enable_task:false,
   })),seeded);
   const selectUrl=base+'/online-scripts/install-select/browser-import';
   const stateUrl=base+'/api/online-scripts/import-tasks-status/'+seeded.request_id;
   await page.route(stateUrl,route=>route.fulfill({status:503,contentType:'text/html',body:'import state temporarily unavailable'}));
   await page.goto(selectUrl);
   await page.waitForFunction(()=>document.querySelector('#onlineInstallRecoveryStatus').textContent.includes('import state temporarily unavailable'));
   assert.equal(await page.locator('#onlineInstallSelectForm [name="request_id"]').inputValue(),seeded.request_id);
   assert.equal(await page.locator('#excludedTaskIndexes').inputValue(),'2');
   assert.equal(await page.locator('.fls-install-task-checkbox[data-index="1"]').isChecked(),true);
   assert.equal(await page.locator('.fls-install-task-checkbox[data-index="2"]').isChecked(),false);
   assert.equal(await page.getByRole('link',{name:'查询原导入请求',exact:true}).isVisible(),true);
   await page.unroute(stateUrl);
   await page.route(stateUrl,route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({ok:true,request_id:'f'.repeat(32),state:'completed'})}));
   await page.locator('#onlineInstallRecoveryRetry').click();
   await page.waitForFunction(()=>document.querySelector('#onlineInstallRecoveryStatus').textContent.includes('导入状态响应无效'));
   assert.equal(await page.evaluate(()=>JSON.parse(sessionStorage.getItem('fls-online-install-request')).request_id),seeded.request_id);
   await page.unroute(stateUrl);
   await page.route(stateUrl,route=>route.fulfill({status:401,contentType:'application/json',body:'{"ok":false,"msg":"session expired"}'}));
   await page.locator('#onlineInstallRecoveryRetry').click();
   await page.locator('#onlineInstallApiLogin').waitFor({state:'visible'});
   await fixture({expire:true});
   await page.locator('#onlineInstallApiLogin').click();
   await page.waitForURL('**/login?next=*');
   await page.unroute(stateUrl);
   await page.locator('[name="token"]').fill(process.env.FLS_UI_TOKEN);
   await page.locator('button[type="submit"]').click();
   await page.waitForURL(selectUrl);
   await page.waitForFunction(()=>document.querySelector('#onlineInstallRecoveryStatus').textContent.includes('未写入任务'));
   assert.equal((await fixture({inspect:true})).reloads,0);
   await page.getByRole('link',{name:'查询原导入请求',exact:true}).click();
   await page.waitForURL(base+'/online-scripts/import-tasks-status/'+seeded.request_id);
   const originalForm=page.locator('form[action="/online-scripts/import-tasks/browser-import"]');
   assert.equal(await originalForm.locator('[name="task_indexes"]').inputValue(),'1');
   await originalForm.getByRole('button',{name:'继续原导入请求',exact:true}).click();
   await page.waitForURL(url=>url.pathname==='/online-scripts'&&url.searchParams.has('msg'));
   assert.equal((await fixture({inspect:true})).tasks.length,1);
   assert.equal((await fixture({inspect:true})).reloads,1);
   await page.goto(selectUrl);
   await page.waitForURL(url=>url.pathname==='/online-scripts'&&url.searchParams.has('msg'));
   assert.equal(await page.evaluate(()=>sessionStorage.getItem('fls-online-install-request')),null);
   assert.equal((await fixture({inspect:true})).tasks.length,1);

   for(const mode of ['before','after']){
     await fixture({fail:false,startup_failure:mode});
     await page.goto(selectUrl);
     await page.locator('.fls-install-task-checkbox[data-index="2"]').uncheck();
     const startupId=await page.locator('#onlineInstallSelectForm [name="request_id"]').inputValue();
     const post=base+'/api/online-scripts/import-tasks/browser-import';
     await page.route(post,async route=>{const response=await route.fetch();assert.equal(response.status(),500);await route.abort('connectionfailed');});
     await page.locator('button[formaction="/online-scripts/import-tasks/browser-import"]').click();
     await page.locator('#onlineInstallApiError').waitFor({state:'visible'});
     assert.equal((await fixture({inspect:true})).tasks.length,0);
     assert.equal((await fixture({inspect:true})).reloads,0);
     assert.equal(await page.evaluate(()=>JSON.parse(sessionStorage.getItem('fls-online-install-request')).request_id),startupId);
     await page.unroute(post);
     await page.reload();
     await page.waitForFunction(()=>document.querySelector('#onlineInstallRecoveryStatus').textContent.includes('browser import startup receipt unavailable'));
     assert.equal(await page.locator('#onlineInstallSelectForm [name="request_id"]').inputValue(),startupId);
     assert.equal(await page.locator('.fls-install-task-checkbox[data-index="2"]').isChecked(),false);
     await page.getByRole('link',{name:'查询原导入请求',exact:true}).click();
     await page.getByRole('button',{name:'继续原导入请求',exact:true}).click();
     await page.waitForURL(url=>url.pathname==='/online-scripts'&&url.searchParams.has('msg'));
     const actual=await fixture({inspect:true});
     assert.equal(actual.tasks.length,1);assert.equal(actual.reloads,1);
     await page.goto(selectUrl);
     await page.waitForURL(url=>url.pathname==='/online-scripts'&&url.searchParams.has('msg'));
     assert.equal(await page.evaluate(()=>sessionStorage.getItem('fls-online-install-request')),null);
   }
   await page.screenshot({path:'/tmp/fls-import-startup-'+viewport.width+'.png'});
   console.log('prepared import, startup failures, original choices and login recovery passed '+viewport.width);
   await fixture();
   await page.goto(base+'/online-scripts/install-select/browser-import');
   const form=page.locator('form[action="/online-scripts/install/browser-import"]');
   const requestId=await form.locator('[name="request_id"]').inputValue();
   assert.match(requestId,/^[a-f0-9]{32}$/);
   await form.locator('button[formaction="/online-scripts/import-tasks/browser-import"]').click();
   await page.waitForFunction(()=>document.body.textContent.includes('任务已持久化，但调度器重载失败'));
   const statusLink=page.getByRole('link',{name:'查询原导入请求',exact:true});
   assert.equal(await statusLink.count(),1);
   await statusLink.click();
   await page.waitForFunction(()=>document.body.textContent.includes('任务已保存，调度器待恢复'));
   const reload=page.getByRole('button',{name:'仅重新加载调度器',exact:true});
   assert.equal(await reload.count(),1);
   await reload.click();
   await page.waitForFunction(()=>document.body.textContent.includes('任务已保存，调度器已恢复'));
   assert.deepEqual(errors,[]);
   console.log('online task import recovery passed '+viewport.width);
   await context.close();
  }
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
'''


if __name__ == "__main__":
    main()
