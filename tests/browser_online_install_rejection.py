"""Real HTTP/Chromium recovery for pre-accepted online install rejections."""
import os
import tempfile
import threading
import subprocess

from tools.browser_regression import TOKEN, find_chromium, find_playwright_core, seed_browser_fixture


def main():
    playwright, chromium = find_playwright_core(), find_chromium()
    if not playwright or not chromium:
        raise SystemExit("Set FLS_PLAYWRIGHT_CORE and FLS_CHROMIUM")
    with tempfile.TemporaryDirectory(prefix="fls-install-rejection-browser-") as temp:
        seed_browser_fixture(temp)
        os.environ.update(FLS_BASE_DIR=temp, FLS_TOKEN=TOKEN, FLS_SECRET_KEY="install-rejection-browser")
        from flask import jsonify
        from werkzeug.serving import make_server
        from fls_manager.app import create_app
        import fls_manager.app as app_module
        from fls_manager.online_scripts import source
        from fls_manager.routes.online_scripts import install as install_route

        item = {
            "id": "browser-install", "name": "浏览器安装拒绝恢复", "type": "raw",
            "link": "https://fixture.invalid/browser-install.py", "link_name": "browser-install.py",
            "task_cron": [{"name": "Keep selection", "command": "echo keep", "cron": ""},
                          {"name": "Excluded selection", "command": "echo exclude", "cron": ""}],
        }
        source.save_online_script_cache([item])
        starts = []

        def unexpected_start(*args, **kwargs):
            starts.append(kwargs.get("request_id", ""))
            return jsonify(ok=False, msg="install start should not be reached"), 500

        real_start = install_route._start_install_job
        install_route._start_install_job = unexpected_start
        app = create_app()
        app_module.dispatch_frontend_open_event = lambda: None

        @app.route("/api/fixture/online-install/<request_id>")
        def inspect_install(request_id):
            return jsonify(
                ok=True,
                starts=starts,
                job=install_route.get_online_install_job(request_id),
            )

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
   const context=await browser.newContext({viewport}),page=await context.newPage(),errors=[];
   page.on('pageerror',error=>errors.push(error.message));
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
   assert.deepEqual(persisted.starts,[]);
   assert.deepEqual(errors,[]);
   await page.screenshot({path:'/tmp/fls-install-rejection-'+viewport.width+'.png'});
   console.log('pre-acceptance install rejection, selection retention, CSRF and zero side effects passed '+viewport.width);
   await context.close();
  }
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exit(1)});
'''


if __name__ == "__main__":
    main()
