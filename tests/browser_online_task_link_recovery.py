"""Real HTTP/Chromium recovery for idempotent online task-link sync."""
import os
import tempfile
import threading
import subprocess
from pathlib import Path

from tools.browser_regression import TOKEN, find_chromium, find_playwright_core, seed_browser_fixture


def main():
    playwright, chromium = find_playwright_core(), find_chromium()
    if not playwright or not chromium:
        raise SystemExit("Set FLS_PLAYWRIGHT_CORE and FLS_CHROMIUM")
    with tempfile.TemporaryDirectory(prefix="fls-task-link-browser-") as temp:
        seed_browser_fixture(temp)
        os.environ.update(FLS_BASE_DIR=temp, FLS_TOKEN=TOKEN, FLS_SECRET_KEY="task-link-browser")
        from flask import jsonify, request
        from werkzeug.serving import make_server
        from fls_manager.app import create_app
        import fls_manager.app as app_module
        from fls_manager import scheduler
        from fls_manager.online_scripts import source
        from fls_manager.online_scripts.constants import update_online_refresh_state
        from fls_manager.routes.online_scripts import task_link

        item = {
            "id": "browser-link", "name": "浏览器任务源", "type": "raw",
            "link": "https://fixture.invalid/browser-link.py", "link_name": "browser-link.py",
            "task_link": "https://fixture.invalid/browser-link.json",
        }
        source.save_online_script_cache([item])
        fault = {"fail": True, "fetches": 0}
        real_fetch = task_link.fetch_task_link_tasks

        def fetch(*_args, **_kwargs):
            fault["fetches"] += 1
            if fault["fail"]:
                raise RuntimeError("fixture task source unavailable")
            return [{"name": "Browser task", "command": "echo browser", "cron": ""}]

        task_link.fetch_task_link_tasks = fetch
        app = create_app()
        app_module.dispatch_frontend_open_event = lambda: None

        @app.route("/api/fixture/task-link", methods=["GET", "POST"])
        def fixture():
            if request.method == "POST":
                data = request.get_json() or {}
                fault["fail"] = bool(data.get("fail"))
                if data.get("reset"):
                    fault["fetches"] = 0
                    source.save_online_script_cache([item])
            cached = source.load_online_script_cache()
            return jsonify(fail=fault["fail"], fetches=fault["fetches"], cached=cached)

        @app.route("/api/fixture/online-refresh", methods=["POST"])
        def refresh_fixture():
            data = request.get_json() or {}
            if data.get("clear"):
                update_online_refresh_state(
                    id="",
                    running=False,
                    status="",
                    message="",
                    error="",
                    cache_published=False,
                    task_link_failures=[],
                    updated_at="",
                    log_file="",
                )
                return jsonify(ok=True)
            update_online_refresh_state(
                id="browser-refresh",
                running=False,
                status="部分完成",
                message="同步在线脚本源部分完成，请逐项重试",
                error="",
                cache_published=True,
                task_link_failures=[{
                    "script_id": item["id"],
                    "name": item["name"],
                    "task_link": item["task_link"],
                    "proxy_id": "",
                    "error": "fixture refresh task source unavailable",
                }],
                updated_at="now",
                log_file="/tmp/browser-refresh.log",
            )
            return jsonify(ok=True)

        server = make_server("127.0.0.1", 0, app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            env = dict(os.environ, FLS_UI_BASE=f"http://127.0.0.1:{server.server_port}",
                       FLS_UI_TOKEN=TOKEN, FLS_PLAYWRIGHT_CORE=str(playwright), FLS_CHROMIUM=str(chromium))
            subprocess.run(["node", "-e", SCRIPT], env=env, check=True, timeout=180)
        finally:
            task_link.fetch_task_link_tasks = real_fetch
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
   const fixture=async data=>{const r=await page.request.post(base+'/api/fixture/task-link',{headers,data});assert.equal(r.status(),200);return r.json();};
   await fixture({reset:true,fail:true});
   const clearRefresh=await page.request.post(base+'/api/fixture/online-refresh',{headers,data:{clear:true}});
   assert.equal(clearRefresh.status(),200);
   await page.goto(base+'/login');
   await page.locator('[name="token"]').fill(process.env.FLS_UI_TOKEN);
   await page.locator('button[type="submit"]').click();
   await page.goto(base+'/online-scripts');
   const form=page.locator('#online-list form[action="/online-scripts/task-link/browser-link"]');
   const requestId=await form.locator('[name="request_id"]').inputValue();
   assert.match(requestId,/^[a-f0-9]{32}$/);
   await form.locator('button[type="submit"]').click();
   const retry=page.getByRole('button',{name:'继续原同步请求',exact:true});
   await retry.waitFor();
   assert.equal(await retry.locator('xpath=ancestor::form').locator('[name="request_id"]').inputValue(),requestId);
   assert.match(await page.locator('.content').textContent(),/fixture task source unavailable/);
   await page.getByRole('link',{name:'查询原同步请求',exact:true}).click();
   await page.waitForFunction(()=>document.body.textContent.includes('请求失败，可继续原同步请求'));
   assert.match(await page.locator('.content').textContent(),/请求失败，可继续原同步请求/);
   await page.goto(base+'/online-scripts');
   const retryAgain=page.getByRole('button',{name:'继续原同步请求',exact:true});
   await fixture({fail:false});
   await retryAgain.click();
   await page.waitForFunction(()=>document.body.textContent.includes('外部任务源同步完成'));
   const state=await fixture({});
   assert.equal(state.fetches,2);
   assert.equal(state.cached[0].task_cron[0].name,'Browser task');

   await fixture({reset:true,fail:true});
   await page.goto(base+'/online-scripts');
   const refreshFixture=await page.request.post(base+'/api/fixture/online-refresh',{headers,data:{}});
   assert.equal(refreshFixture.status(),200);
   await page.evaluate(async()=>{await updateOnlineRefreshStatus();});
   const dynamicForm=page.locator('#onlineRefreshTaskLinkFailures form.fls-online-task-link-form');
   assert.equal(await dynamicForm.count(),1);
   const dynamicRequestId=await dynamicForm.locator('[name="request_id"]').inputValue();
   assert.match(dynamicRequestId,/^[a-f0-9]{32}$/);
   await dynamicForm.locator('button[type="submit"]').click();
   const dynamicRetry=page.getByRole('button',{name:'继续原同步请求',exact:true});
   await dynamicRetry.waitFor();
   assert.equal(await dynamicRetry.locator('xpath=ancestor::form').locator('[name="request_id"]').inputValue(),dynamicRequestId);
   assert.match(await page.locator('.content').textContent(),/fixture refresh task source unavailable/);
   await fixture({fail:false});
   await dynamicRetry.click();
   await page.waitForFunction(()=>document.body.textContent.includes('外部任务源同步完成'));
   const dynamicState=await fixture({});
   assert.equal(dynamicState.fetches,2);
   assert.deepEqual(errors,[]);
   console.log('online task-link recovery passed '+viewport.width);
   await context.close();
  }
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
'''


if __name__ == "__main__":
    main()
