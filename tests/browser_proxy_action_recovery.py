"""Isolated real HTTP/Chromium checks for proxy actions and task references."""
import os
import subprocess
import tempfile
import threading

from tools.browser_regression import seed_browser_fixture, TOKEN, find_playwright_core, find_chromium


def main():
    playwright, chromium = find_playwright_core(), find_chromium()
    if not playwright or not chromium:
        raise SystemExit("Set FLS_PLAYWRIGHT_CORE and FLS_CHROMIUM")
    with tempfile.TemporaryDirectory(prefix="fls-proxy-browser-") as temp:
        seed_browser_fixture(temp)
        os.environ.update(FLS_BASE_DIR=temp, FLS_TOKEN=TOKEN, FLS_SECRET_KEY="proxy-browser-secret")
        from flask import jsonify, request
        from werkzeug.serving import make_server
        from fls_manager.app import create_app
        from fls_manager import models, proxy_actions, scheduler
        from fls_manager.config import save_config
        from fls_manager.routes.proxy import actions

        save_config({"notify_items": [], "notify_default_ids": []})
        app = create_app()
        fault = {}
        save, receipt = models.save_proxies, proxy_actions.write_json

        def persist(values):
            if fault.get("save"):
                raise OSError("fixture save rejected")
            save(values)
            if fault.get("after"):
                raise OSError("fixture confirmation rejected")

        def write(path, row):
            if fault.get("receipt") and row["state"] != "prepared":
                raise OSError("fixture receipt rejected")
            return receipt(path, row)

        models.save_proxies = actions.save_proxies = persist
        proxy_actions.write_json = write

        @app.route("/api/fixture/proxy", methods=["GET", "POST"])
        def fixture():
            if request.method == "GET":
                return jsonify(proxies=models.load_proxies(), tasks=models.load_tasks(), history=models.load_task_history())
            data = request.get_json()
            fault.clear()
            fault.update(data)
            if data.get("reset"):
                save([{"id": "p", "name": "Browser proxy", "type": "http", "host": "127.0.0.1", "port": "8080", "enabled": True}])
                models.save_tasks([{"id": "run", "name": "Referenced task", "command": "echo proxy restored", "proxy_id": "p" if data.get("reference") else "", "enabled": True}])
            if data.get("changed"):
                values = models.load_proxies()
                values[0]["name"] = "Changed proxy"
                save(values)
            if data.get("unavailable"):
                values = models.load_proxies()
                values[0]["enabled"] = False
                save(values)
            return jsonify(ok=True)

        server = make_server("127.0.0.1", 0, app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        env = dict(os.environ, FLS_UI_BASE=f"http://127.0.0.1:{server.server_port}", FLS_UI_TOKEN=TOKEN,
                   FLS_PLAYWRIGHT_CORE=str(playwright), FLS_CHROMIUM=str(chromium))
        try:
            result = subprocess.run(["node", "-e", SCRIPT], env=env, timeout=180)
            if result.returncode:
                raise SystemExit(result.returncode)
        finally:
            server.shutdown()
            scheduler.scheduler.shutdown(wait=False)


SCRIPT = r'''
const assert=require('node:assert/strict');
const {chromium}=require(process.env.FLS_PLAYWRIGHT_CORE);
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.FLS_CHROMIUM,args:['--no-sandbox']});
 try {
  for(const viewport of [{width:390,height:844},{width:1280,height:800}]){
   const context=await browser.newContext({viewport});
   const page=await context.newPage();
   page.on('dialog',d=>d.accept());
   const errors=[];
   page.on('pageerror',e=>errors.push(e.message));
   const base=process.env.FLS_UI_BASE,headers={'X-Token':process.env.FLS_UI_TOKEN};
   const fixture=async data=>assert.equal((await page.request.post(base+'/api/fixture/proxy',{headers,data})).status(),200);
   const state=async()=>(await page.request.get(base+'/api/fixture/proxy',{headers})).json();
   const login=async()=>{
    await page.locator('input[name="token"]').fill(process.env.FLS_UI_TOKEN);
    await page.locator('button[type="submit"]').click();
    await page.waitForURL(u=>u.pathname!='/login');
   };
   const open=async()=>page.goto(base+'/proxy');
   const form=async action=>{
    if(viewport.width===390 && action==='toggle'){
     const more=page.locator('#proxyMobileList .proxy-mobile-more > summary').first();
     if(await more.isVisible()) await more.click();
    }
    return page.locator('form[action="/proxy/'+action+'/p"]:visible').first();
   };
   const submit=async f=>{
    const path=new URL(await f.getAttribute('action'),base).pathname;
    const response=page.waitForResponse(r=>new URL(r.url()).pathname===path && r.request().method()==='POST');
    await f.locator('button[type="submit"]').click();
    const res=await response;
    await page.waitForFunction(()=>!document.querySelector('.fls-loading-overlay.active'));
    return res;
   };
   const notice=async text=>{await page.locator('[data-proxy-result]').waitFor();assert.match(await page.locator('[data-proxy-result]').innerText(),text);};
   await fixture({reset:true});
   await page.goto(base+'/login');await login();await open();
   await fixture({save:true});
   assert.equal((await submit(await form('toggle'))).status(),500);
   await notice(/保存失败/);
   assert.equal((await state()).proxies[0].enabled,true);
   await fixture({after:true});
   assert.equal((await submit(page.locator('form[action="/proxy/toggle/p"]'))).status(),503);
   await notice(/已保存/);
   assert.equal((await state()).proxies[0].enabled,false);
   await fixture({});await open();
   await submit(await form('toggle'));
   await page.waitForURL(u=>u.pathname==='/proxy');
   assert.equal((await state()).proxies[0].enabled,true);

   // Invalid CSRF and stale selection must not create any change.
   await open();
   const stale=await (await form('delete')).evaluate(f=>Object.fromEntries(new FormData(f)));
   assert.equal((await page.request.post(base+'/proxy/delete/p',{form:{...stale,csrf_token:'bad'}})).status(),400);
   await fixture({changed:true});
   assert.equal((await submit(await form('delete'))).status(),409);
   await notice(/已变化/);
   assert.equal((await state()).proxies[0].name,'Changed proxy');

   // References are actionable, and the original unavailable option is retained.
   await fixture({reset:true,reference:true});await open();
   assert.equal((await submit(await form('toggle'))).status(),409);
   await notice(/任务引用/);
   await page.locator('[href="/task/edit/run"]').click();
   await page.waitForURL(u=>u.pathname==='/task/edit/run');
   assert.equal(await page.locator('select[name="proxy_id"]').inputValue(),'p');
   await open();
   assert.equal((await submit(await form('delete'))).status(),409);
   await notice(/任务引用/);
   await fixture({unavailable:true});
   const failed=await page.request.post(base+'/api/task/action/run/run',{headers,data:{request_id:'b'.repeat(32)}});
   assert.equal(failed.status(),500);assert.match((await failed.json()).msg,/代理/);
   await page.goto(base+'/log/run');
   const runForm=page.locator('form[action^="/run/run"]:visible').first();
   assert.equal((await submit(runForm)).status(),500);
   await page.getByRole('link',{name:'修复所选代理',exact:true}).first().click();
   await page.waitForURL(u=>u.pathname==='/proxy');
   await page.goto(base+'/task/edit/run');
   assert.equal(await page.locator('select[name="proxy_id"]').inputValue(),'p');
   // Enable remains available while referenced; restored execution succeeds.
   await open();await submit(await form('toggle'));
   assert.equal((await page.request.post(base+'/api/task/action/run/run',{headers,data:{request_id:'c'.repeat(32)}})).status(),200);
   await page.waitForTimeout(150);
   const actual=await state();assert.ok(actual.history.some(h=>h.task_id==='run'&&h.status==='success'));
   await page.goto(base+'/task/edit/run');
   await page.locator('select[name="proxy_id"]').selectOption('');
   await page.locator('button[type="submit"]').filter({hasText:'保存任务'}).click();
   await page.waitForURL(u=>u.pathname!='/task/edit/run');
   assert.equal((await state()).tasks[0].proxy_id,'');

   // Drop the response after publication, refresh and query the exact old request.
   await open();
   await page.route('**/proxy/toggle/p',async route=>{await route.fetch();await route.abort();},{times:1});
   await (await form('toggle')).locator('button').click();
   await page.locator('[data-collection-save-pending]').waitFor();
   await page.reload();
   await page.locator('[data-collection-save-pending]').waitFor();
   await page.getByRole('button',{name:'查询本次保存结果'}).click();
   await page.waitForFunction(()=>!document.querySelector('[data-collection-save-pending]'));
   assert.equal((await state()).proxies[0].enabled,false);

   // An interrupted write requires explicit confirmation, then deletion is idempotent.
   await fixture({save:true,receipt:true});await open();
   assert.equal((await submit(await form('delete'))).status(),500);
   await notice(/确认后重试/);
   await fixture({});
   await submit(page.locator('form[action="/proxy/delete/p"]'));
   await page.waitForURL(u=>u.pathname==='/proxy');
   assert.equal((await state()).proxies.length,0);

   // Non-JSON failure and session renewal preserve the original submission.
   await fixture({reset:true});await open();
   await page.route('**/proxy/delete/p',r=>r.fulfill({status:502,contentType:'text/plain',body:'fixture gateway'}),{times:1});
   await (await form('delete')).locator('button').click();
   await page.locator('[data-collection-save-pending]').waitFor();
   assert.equal((await state()).proxies.length,1);
   await page.goto(base+'/logout');await login();await open();
   await page.getByRole('button',{name:'查询本次保存结果'}).click();
   await page.waitForFunction(()=>!document.querySelector('[data-collection-save-pending]'));
   assert.equal((await state()).proxies.length,0);
   assert.deepEqual(errors,[]);
   console.log('proxy recovery passed '+viewport.width);
   await fixture({reset:true});await open();await fixture({save:true});
   await submit(await form('toggle'));await notice(/保存失败/);
   if(process.env.FLS_BULK_SCREENSHOTS) await page.screenshot({path:process.env.FLS_BULK_SCREENSHOTS+'/proxy-recovery-'+viewport.width+'.png',fullPage:true});
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
   await context.close();
  }
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
'''


if __name__ == "__main__":
    main()
