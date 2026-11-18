"""Real HTTP/Chromium recovery for idempotent online task import."""
import os
import tempfile
import threading
import subprocess

from tools.browser_regression import TOKEN, find_chromium, find_playwright_core, seed_browser_fixture


def main():
    playwright, chromium = find_playwright_core(), find_chromium()
    if not playwright or not chromium:
        raise SystemExit("Set FLS_PLAYWRIGHT_CORE and FLS_CHROMIUM")
    with tempfile.TemporaryDirectory(prefix="fls-task-import-browser-") as temp:
        seed_browser_fixture(temp)
        os.environ.update(FLS_BASE_DIR=temp, FLS_TOKEN=TOKEN, FLS_SECRET_KEY="task-import-browser")
        from flask import jsonify, request
        from werkzeug.serving import make_server
        from fls_manager.app import create_app
        import fls_manager.app as app_module
        from fls_manager import scheduler
        from fls_manager.models import save_tasks
        from fls_manager.online_scripts import source, tasks
        from fls_manager.routes.online_scripts import install_select

        item = {
            "id": "browser-import", "name": "浏览器任务导入", "type": "raw",
            "link": "https://fixture.invalid/browser-import.py", "link_name": "browser-import.py",
            "task_cron": [{"name": "Browser task", "command": "echo browser", "cron": ""}],
        }
        source.save_online_script_cache([item])
        fault = {"fail": True}

        def task_reload():
            if fault["fail"]:
                raise RuntimeError("fixture scheduler unavailable")

        def recovery_reload():
            return None

        real_task_reload = tasks.reload_scheduler
        real_recovery_reload = install_select.reload_scheduler
        tasks.reload_scheduler = task_reload
        install_select.reload_scheduler = recovery_reload
        app = create_app()
        app_module.dispatch_frontend_open_event = lambda: None

        @app.route("/api/fixture/task-import", methods=["POST"])
        def fixture():
            source.save_online_script_cache([item])
            save_tasks([])
            fault["fail"] = True
            return jsonify(ok=True)

        server = make_server("127.0.0.1", 0, app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            env = dict(os.environ, FLS_UI_BASE=f"http://127.0.0.1:{server.server_port}",
                       FLS_UI_TOKEN=TOKEN, FLS_PLAYWRIGHT_CORE=str(playwright), FLS_CHROMIUM=str(chromium))
            subprocess.run(["node", "-e", SCRIPT], env=env, check=True, timeout=180)
        finally:
            tasks.reload_scheduler = real_task_reload
            install_select.reload_scheduler = real_recovery_reload
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
   const fixture=async()=>{const r=await page.request.post(base+'/api/fixture/task-import',{headers});assert.equal(r.status(),200);};
   await fixture();
   await page.goto(base+'/login');
   await page.locator('[name="token"]').fill(process.env.FLS_UI_TOKEN);
   await page.locator('button[type="submit"]').click();
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
