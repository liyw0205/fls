"""Isolated HTTP/Chromium regression: python -m tests.browser_task_bulk_recovery."""
import os
import subprocess
import tempfile
import threading

from tools.browser_regression import seed_browser_fixture, TOKEN, find_playwright_core, find_chromium


def main():
    playwright = find_playwright_core()
    chromium = find_chromium()
    if not playwright or not chromium:
        raise SystemExit("Set FLS_PLAYWRIGHT_CORE and FLS_CHROMIUM to local browser dependencies")
    with tempfile.TemporaryDirectory(prefix="fls-bulk-browser-") as temp_dir:
        seed_browser_fixture(temp_dir)
        os.environ.update(FLS_BASE_DIR=temp_dir, FLS_TOKEN=TOKEN, FLS_SECRET_KEY="bulk-browser-secret")
        from flask import jsonify, request
        from werkzeug.serving import make_server
        from fls_manager.app import create_app
        from fls_manager import models, scheduler
        from fls_manager.routes import api
        from fls_manager.config import save_config

        save_config({"notify_items": [], "notify_default_ids": []})
        app = create_app()
        fault = {"reload": False, "save": False, "unknown": False}
        reload_scheduler = api.reload_scheduler
        mutate_tasks = api.mutate_tasks
        run_task = api.run_task_now

        def run(*args, **kwargs):
            result = run_task(*args, **kwargs)
            if fault["unknown"] and args[0] == "a":
                raise RuntimeError("fixture after submission")
            return result

        def reload():
            if fault["reload"]:
                raise RuntimeError("fixture reload rejected")
            return reload_scheduler()

        def mutate(*args, **kwargs):
            if fault["save"]:
                raise OSError("fixture save rejected")
            return mutate_tasks(*args, **kwargs)

        api.reload_scheduler = reload
        api.mutate_tasks = mutate
        api.run_task_now = run

        @app.route("/api/fixture/bulk", methods=["POST", "GET"])
        def fixture():
            if request.method == "GET":
                return jsonify(tasks=models.load_tasks(), history=models.load_task_history())
            data = request.get_json()
            fault.update(reload=bool(data.get("reload")), save=bool(data.get("save")), unknown=bool(data.get("unknown")))
            if data.get("reset"):
                models.save_tasks([{
                    "id": task_id, "name": "Bulk " + task_id, "command": "echo bulk" if task_id == "a" else "task missing.py",
                    "enabled": True, "collection_id": "browser-collection" if data.get("collection") else "", "cron": "0 8 * * *",
                } for task_id in ("a", "b")])
                scheduler.reload_scheduler()
            if data.get("fix"):
                def fix(tasks):
                    for task in tasks:
                        task["command"] = "echo bulk"
                    return True
                models.mutate_tasks(fix)
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
const assert = require('node:assert/strict');
const {chromium} = require(process.env.FLS_PLAYWRIGHT_CORE);
(async () => {
 const browser = await chromium.launch({executablePath:process.env.FLS_CHROMIUM,args:['--no-sandbox']});
 try {
  for(const viewport of [{width:390,height:844},{width:1280,height:800}]){
   for(const path of ['/tasks','/collections']){
    const context = await browser.newContext({viewport});
    const page = await context.newPage();
    page.on('dialog', dialog => dialog.accept());
    const errors=[];
    page.on('pageerror', error => errors.push(error.message));
    const base=process.env.FLS_UI_BASE;
    const headers={'X-Token':process.env.FLS_UI_TOKEN};
    const fixture=async data => {
     const response=await page.request.post(base+'/api/fixture/bulk',{headers,data});
     assert.equal(response.status(),200);
    };
    const state=async () => (await page.request.get(base+'/api/fixture/bulk',{headers})).json();
    const login=async () => {
     await page.locator('input[name="token"]').fill(process.env.FLS_UI_TOKEN);
     await page.locator('button[type="submit"]').click();
     await page.waitForURL(url => url.pathname !== '/login');
    };
    const box=path==='/tasks'?'.task-select-checkbox':'.collection-task-select';
    const choose=async ids => {
     for(const id of ids) await page.locator(box+'[data-task-id="'+id+'"]:visible').first().check();
    };
    const selected=async () => page.locator(box+':checked').evaluateAll(boxes=>Array.from(new Set(boxes.map(b=>b.dataset.taskId))).sort());
    const control=action => page.locator((path==='/tasks'?'.task-bulk-btn':'.collection-bulk-btn')+'[onclick*="\''+action+'\'"]:visible').first();
    const click=async action => {
     const response=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/task/bulk-action');
     await control(action).click();
     return await response;
    };
    const settled=async () => page.waitForFunction(()=>!window.__flsTaskBulkBusy);
    await fixture({reset:true,collection:path==='/collections'});
    await page.goto(base+'/login');
    await login();
    await page.goto(base+path);
    if(process.env.FLS_BULK_EXTRA_ONLY){
     await choose(['a','b']);
     const original=await state();
     await page.route('**/api/task/bulk-action',route=>route.fulfill({status:502,contentType:'text/html',body:'upstream unavailable'}));
     assert.equal((await click('run')).status(),502);
     await settled();
     assert.deepEqual((await state()).tasks,original.tasks);
     assert.deepEqual(await selected(),['a','b']);
     await page.unroute('**/api/task/bulk-action');
     await page.getByRole('button',{name:'查询本次操作结果',exact:true}).click();
     await settled();
     assert.deepEqual(await selected(),['b']);
     const rejected=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/task/bulk-action');
     await page.getByRole('button',{name:'重试未完成项',exact:true}).click();
     assert.equal((await rejected).status(),400);
     await settled();
     assert.deepEqual(await selected(),['b']);
     await fixture({unknown:true});
     await choose(['a']);
     const unknown=await click('run');
     assert.deepEqual((await unknown.json()).unknown_ids,['a']);
     await settled();
     assert.deepEqual(await selected(),['b']);
     await page.getByRole('link',{name:'查看任务历史',exact:true}).waitFor();
     await page.getByRole('button',{name:'重新选择结果未知项',exact:true}).click();
     assert.deepEqual(await selected(),['a','b']);
     await page.locator(box+'[data-task-id="b"]:visible').first().uncheck();
     await fixture({save:true});
     const failedDelete=await click('delete');
     assert.equal(failedDelete.status(),500);
     assert.deepEqual((await failedDelete.json()).stopped_ids,['a']);
     await settled();
     assert.deepEqual(await selected(),['a']);
     assert.ok((await state()).tasks.find(t=>t.id==='a'));
     await fixture({});
     await page.getByRole('button',{name:'重试未完成项',exact:true}).click();
     await settled();
     assert.equal((await state()).tasks.find(t=>t.id==='a'),undefined);
     assert.deepEqual(await selected(),[]);
     assert.equal(await page.locator(box+'[data-task-id="a"]').count(),0);
     await choose(['b']);
     await fixture({reload:true});
     assert.equal((await click('disable')).status(),503);
     await settled();
     await fixture({});
     await page.route('**'+path,route=>route.request().headers()['x-requested-with']
      ? route.fulfill({status:503,body:'refresh rejected'}) : route.continue());
     await page.getByRole('button',{name:'重新加载调度器',exact:true}).click();
     await page.getByText(/调度器已重新加载，页面恢复失败/).waitFor();
     assert.equal(await page.getByRole('button',{name:'重新加载调度器',exact:true}).count(),0);
     await page.unroute('**'+path);
     await page.getByRole('button',{name:'刷新列表',exact:true}).click();
     await page.getByText('已重新加载调度器',{exact:true}).waitFor();
     assert.deepEqual(errors,[]);
     console.log(JSON.stringify({path,viewport,extra:true,status:'passed'}));
     await context.close();
     continue;
    }
    await choose(['a','b']);
    let rejectGet=true;
    await page.route('**'+path, async route=>{
     if(rejectGet && route.request().headers()['x-requested-with']){
      rejectGet=false;
      return route.fulfill({status:503,body:'fixture refresh rejected'});
     }
     return route.continue();
    });
    let response=await click('run');
    assert.equal(response.status(),200);
    assert.deepEqual((await response.json()).completed_ids,['a']);
    await settled();
    assert.deepEqual(await selected(),['b']);
    await page.getByText(/列表刷新失败/).waitFor();
    await page.unroute('**'+path);
    await page.getByRole('button',{name:'刷新列表',exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector('[data-bulk-recovery]').textContent.includes('列表刷新失败'));
    assert.deepEqual(await selected(),['b']);
    await fixture({fix:true});
    response=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/task/bulk-action');
    await page.getByRole('button',{name:'重试未完成项',exact:true}).click();
    assert.deepEqual((await (await response).json()).completed_ids,['b']);
    await settled();
    assert.deepEqual(await selected(),[]);
    assert.equal((await state()).tasks.find(t=>t.id==='a').run_count,1);

    // The POST executes, but its response is lost. A full page reload must retain the key.
    await choose(['a']);
    let dropped=false;
    await page.route('**/api/task/bulk-action',async route=>{
     if(!dropped){ dropped=true; await route.fetch(); return route.abort(); }
     return route.continue();
    });
    await control('run').click();
    await page.getByRole('button',{name:'查询本次操作结果',exact:true}).waitFor();
    await settled();
    const count=(await state()).tasks.find(t=>t.id==='a').run_count;
    await page.reload();
    await page.getByRole('button',{name:'查询本次操作结果',exact:true}).click();
    await settled();
    assert.deepEqual(await selected(),[]);
    assert.equal((await state()).tasks.find(t=>t.id==='a').run_count,count);
    await page.unroute('**/api/task/bulk-action');

    await choose(['a']);
    await fixture({reload:true});
    response=await click('disable');
    assert.equal(response.status(),503);
    assert.equal((await response.json()).saved,true);
    await settled();
    assert.deepEqual(await selected(),[]);
    const saved=(await state()).tasks;
    assert.equal(saved.find(t=>t.id==='a').enabled,false);
    if(process.env.FLS_BULK_SCREENSHOTS) await page.screenshot({path:process.env.FLS_BULK_SCREENSHOTS+'/bulk-'+path.slice(1)+'-'+viewport.width+'.png',fullPage:true});
    await page.getByRole('button',{name:'重新加载调度器',exact:true}).click();
    await page.getByText(/调度器重载失败/).waitFor();
    await fixture({});
    const recovery=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/scheduler/reload');
    await page.getByRole('button',{name:'重新加载调度器',exact:true}).click();
    assert.equal((await recovery).status(),200);
    await page.getByRole('button',{name:'重新加载调度器',exact:true}).waitFor({state:'detached'});
    assert.deepEqual((await state()).tasks,saved);

    await choose(['a']);
    await fixture({save:true});
    response=await click('enable');
    assert.equal(response.status(),500);
    await settled();
    assert.deepEqual(await selected(),['a']);
    await fixture({});
    await page.getByRole('button',{name:'重试未完成项',exact:true}).click();
    await settled();
    assert.equal((await state()).tasks.find(t=>t.id==='a').enabled,true);

    // CSRF and expiry errors keep a queryable original request without starting anything.
    await choose(['b']);
    const before=await state();
    const csrf=await page.locator('meta[name="csrf-token"]').getAttribute('content');
    await page.locator('meta[name="csrf-token"]').evaluate(el=>el.content='invalid');
    response=await click('run');
    assert.equal(response.status(),400);
    await settled();
    assert.deepEqual((await state()).tasks,before.tasks);
    await page.locator('meta[name="csrf-token"]').evaluate((el,csrf)=>el.content=csrf,csrf);
    await page.request.get(base+'/logout');
    await page.getByRole('button',{name:'查询本次操作结果',exact:true}).click();
    await settled();
    await page.getByRole('link',{name:'重新登录',exact:true}).click();
    await login();
    await page.getByRole('button',{name:'查询本次操作结果',exact:true}).click();
    await settled();
    assert.deepEqual(await selected(),[]);
    assert.equal((await state()).tasks.find(t=>t.id==='b').run_count,before.tasks.find(t=>t.id==='b').run_count+1);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({path,viewport,status:'passed'}));
    await context.close();
   }
  }
 } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exit(1);});
'''


if __name__ == "__main__":
    main()
