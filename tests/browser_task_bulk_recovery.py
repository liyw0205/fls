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
        from fls_manager.routes.tasks import actions
        from fls_manager.routes.tasks import collections as collection_routes
        from fls_manager.routes import dashboard
        from fls_manager.config import save_config

        save_config({"notify_items": [], "notify_default_ids": []})
        app = create_app()
        fault = {"reload": False, "save": False, "save_after": False, "unknown": False, "start_fail": False}
        reload_scheduler = api.reload_scheduler
        mutate_tasks = api.mutate_tasks
        run_task = api.run_task_now
        stop_task = api.stop_task_now

        def run(*args, **kwargs):
            if fault["start_fail"]:
                return False, "任务启动失败：fixture thread rejected"
            result = run_task(*args, **kwargs)
            if fault["unknown"] and args[0] == "a":
                raise RuntimeError("fixture after submission")
            return result

        def reload():
            if fault["reload"]:
                raise RuntimeError("fixture reload rejected")
            return reload_scheduler()

        def stop(*args, **kwargs):
            result = stop_task(*args, **kwargs)
            if fault["unknown"] and args[0] == "a":
                raise RuntimeError("fixture after stop")
            return result

        def mutate(*args, **kwargs):
            if fault["save"]:
                raise OSError("fixture save rejected")
            result = mutate_tasks(*args, **kwargs)
            if fault["save_after"]:
                raise OSError("fixture after publication")
            return result

        api.reload_scheduler = reload
        api.mutate_tasks = mutate
        api.run_task_now = run
        api.stop_task_now = stop
        actions.reload_scheduler = reload
        actions.mutate_tasks = mutate
        actions.run_task_now = run
        actions.stop_task_now = stop
        collection_routes.mutate_tasks = mutate
        dashboard.reload_scheduler = reload

        @app.route("/api/fixture/bulk", methods=["POST", "GET"])
        def fixture():
            if request.method == "GET":
                return jsonify(tasks=models.load_tasks(), history=models.load_task_history(), collections=models.load_collections())
            data = request.get_json()
            fault.update(reload=bool(data.get("reload")), save=bool(data.get("save")), save_after=bool(data.get("save_after")), unknown=bool(data.get("unknown")), start_fail=bool(data.get("start_fail")))
            if data.get("reset"):
                if not models.get_collection("browser-collection"):
                    models.save_collections(models.load_collections() + [{"id": "browser-collection", "name": "浏览器回归合集"}])
                models.save_tasks([{
                    "id": task_id, "name": "Bulk " + task_id, "command": ("sleep 30" if data.get("long") else "echo bulk") if task_id == "a" else "task missing.py",
                    "enabled": True, "collection_id": "browser-collection" if data.get("collection") else "", "cron": "0 8 * * *",
                    "config_path": "browser-config.yml" if data.get("config") else "",
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
            from fls_manager.task_runner import running_task_snapshot, stop_task_now
            for task_id in running_task_snapshot():
                stop_task_now(task_id)
            scheduler.scheduler.shutdown(wait=False)


SCRIPT = r'''
const assert = require('node:assert/strict');
const {chromium} = require(process.env.FLS_PLAYWRIGHT_CORE);
(async () => {
 const browser = await chromium.launch({executablePath:process.env.FLS_CHROMIUM,args:['--no-sandbox']});
 try {
  for(const viewport of [{width:390,height:844},{width:1280,height:800}]){
   for(const path of ['/tasks','/collections']){
    if(process.env.FLS_BULK_CASES && !process.env.FLS_BULK_CASES.split(',').includes(path.slice(1)+':'+viewport.width)) continue;
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
    if(process.env.FLS_ADD_COLLECTION_ONLY){
     const target='browser-collection';
     const id=()=>require('node:crypto').randomBytes(16).toString('hex');
     const form=()=>page.locator('form[action^="/collection/add-task/'+target+'"]');
     const notice=()=>page.locator('[data-bulk-recovery="collection-add:'+target+'"]');
     const members=async ()=>(await state()).tasks.map(t=>[t.id,t.collection_id]);
     const add=async (ids,status)=>{
      await form().locator('select[name="task_ids"]').selectOption(ids);
      const response=page.waitForResponse(r=>r.request().method()==='POST' && new URL(r.url()).pathname==='/collection/add-task/'+target);
      await form().getByRole('button',{name:'放入合集',exact:true}).click();
      assert.equal((await response).status(),status);
     };
     await fixture({reset:true});
     await page.goto(base+'/collections');
     await fixture({save:true});
     await add(['a','b'],500);
     await page.getByRole('button',{name:'重试加入合集',exact:true}).waitFor();
     assert.deepEqual(await members(),[['a',''],['b','']]);
     assert.equal(await form().locator('input[name="task_ids"]').count(),2);
     await fixture({});
     const retryResponse=page.waitForResponse(r=>r.request().method()==='POST' && new URL(r.url()).pathname==='/collection/add-task/'+target);
     await page.getByRole('button',{name:'重试加入合集',exact:true}).click();
     assert.equal((await retryResponse).status(),302);
     await form().locator('select[name="task_ids"]').waitFor();
     assert.deepEqual(await members(),[['a',target],['b',target]]);
     await fixture({reset:true,save_after:true});
     await page.goto(base+'/collections');
     await add(['a','b'],503);
     await page.getByText(/任务已保存，后续应用失败/).first().waitFor();
     assert.deepEqual(await members(),[['a',target],['b',target]]);
     assert.equal(await page.getByRole('button',{name:'重试加入合集',exact:true}).count(),0);
     assert.equal(await page.getByRole('button',{name:'重新加载调度器',exact:true}).count(),0);
     await fixture({reset:true});
     await page.goto(base+'/collections');
     const routeMatch=url=>url.pathname==='/collection/add-task/'+target;
     let dropped=false;
     await page.route(routeMatch,async route=>{
      if(!dropped){dropped=true;await route.fetch();return route.abort();}
      return route.continue();
     });
     await form().locator('select[name="task_ids"]').selectOption(['a','b']);
     await form().getByRole('button',{name:'放入合集',exact:true}).click();
     await notice().getByRole('button',{name:'查询本次操作结果',exact:true}).waitFor();
     assert.deepEqual(await members(),[['a',target],['b',target]]);
     assert.equal((await page.request.post(base+'/collection/new',{headers,form:{name:'Moved '+viewport.width}})).status(),200);
     const later=(await state()).collections.find(c=>c.name==='Moved '+viewport.width);
     assert.ok(later);
     assert.equal((await page.request.post(base+'/api/collection/add-task/'+later.id,{headers,data:{task_ids:['b'],expected_collections:{b:target},request_id:id()}})).status(),200);
     await page.reload();
     await notice().getByRole('button',{name:'查询本次操作结果',exact:true}).click();
     await settled();
     assert.deepEqual(await members(),[['a',target],['b',later.id]]);
     await page.unroute(routeMatch);
     await fixture({reset:true});
     await page.goto(base+'/collections');
     assert.equal((await page.request.post(base+'/api/collection/add-task/'+later.id,{headers,data:{task_ids:['b'],expected_collections:{b:''},request_id:id()}})).status(),200);
     await add(['a','b'],409);
     await notice().getByRole('button',{name:'重试未完成项',exact:true}).waitFor();
     assert.deepEqual(await members(),[['a',''],['b',later.id]]);
     await notice().getByRole('button',{name:'重试未完成项',exact:true}).click();
     await settled();
     assert.deepEqual(await members(),[['a',''],['b',later.id]]);
     await fixture({reset:true});
     await page.goto(base+'/collections');
     let rejected=false;
     await page.route(routeMatch,route=>{
      if(!rejected){rejected=true;return route.fulfill({status:502,contentType:'text/plain',body:'fixture form unavailable'});}
      return route.continue();
     });
     await add(['a'],502);
     await notice().getByRole('button',{name:'查询本次操作结果',exact:true}).waitFor();
     assert.deepEqual(await members(),[['a',''],['b','']]);
     await page.goto(base+'/logout');
     await login();
     await page.goto(base+'/collections');
     await notice().getByRole('button',{name:'查询本次操作结果',exact:true}).click();
     await settled();
     assert.deepEqual(await members(),[['a',target],['b','']]);
     await page.unroute(routeMatch);
     await notice().scrollIntoViewIfNeeded();
     if(process.env.FLS_BULK_SCREENSHOTS) await page.screenshot({path:process.env.FLS_BULK_SCREENSHOTS+'/collection-add-'+viewport.width+'.png'});
     await fixture({reset:true});
     await page.goto(base+'/collections');
     assert.equal((await page.request.post(base+'/collection/delete/'+target,{headers,form:{}})).status(),200);
     await add(['a','b'],404);
     await notice().getByRole('link',{name:'重新选择合集和任务',exact:true}).waitFor();
     assert.equal(await notice().getByRole('button',{name:'查询本次操作结果',exact:true}).count(),0);
     assert.equal(await notice().getByRole('button',{name:'重试未完成项',exact:true}).count(),0);
     assert.deepEqual(await members(),[['a',''],['b','']]);
     await notice().getByRole('link',{name:'重新选择合集和任务',exact:true}).click();
     await page.getByRole('link',{name:'新建任务合集',exact:true}).waitFor();
     assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
     assert.deepEqual(errors,[]);
     console.log(JSON.stringify({path,viewport,addCollection:true,status:'passed'}));
     await context.close();
     continue;
    }
    if(process.env.FLS_CLEAR_COLLECTION_ONLY){
     const form=()=>page.locator('form[action^="/task/collection/clear/a"]');
     const notice=()=>page.locator('[data-bulk-recovery="task:a"]');
     const readTask=async ()=>(await state()).tasks.find(t=>t.id==='a');
     const clear=async status=>{
      const response=page.waitForResponse(r=>r.request().method()==='POST' && new URL(r.url()).pathname==='/task/collection/clear/a');
      await form().getByRole('button',{name:'移出合集',exact:true}).click();
      assert.equal((await response).status(),status);
     };
     assert.equal(await form().locator('input[name="expected_collection"]').inputValue(),'browser-collection');
     await fixture({save:true});
     await clear(500);
     await page.getByRole('button',{name:'重试移出合集',exact:true}).waitFor();
     assert.equal((await readTask()).collection_id,'browser-collection');
     await fixture({});
     await page.getByRole('button',{name:'重试移出合集',exact:true}).click();
     await page.waitForURL(url=>url.pathname==='/collections');
     assert.equal((await readTask()).collection_id,'');
     await fixture({reset:true,collection:true});
     await page.goto(base+'/collections');
     const match=url=>url.pathname==='/task/collection/clear/a';
     let dropped=false;
     await page.route(match,async route=>{
      if(!dropped){dropped=true;await route.fetch();return route.abort();}
      return route.continue();
     });
     await form().getByRole('button',{name:'移出合集',exact:true}).click();
     await notice().getByRole('button',{name:'查询本次操作结果',exact:true}).waitFor();
     assert.equal((await readTask()).collection_id,'');
     await page.reload();
     assert.equal(await form().count(),0);
     await notice().getByRole('button',{name:'查询本次操作结果',exact:true}).click();
     await settled();
     assert.equal((await readTask()).collection_id,'');
     await page.unroute(match);
     await fixture({reset:true,collection:true});
     await page.goto(base+'/collections');
     assert.equal((await page.request.post(base+'/collection/new',{headers,form:{name:'Later collection',remark:'fixture'}})).status(),200);
     const later=(await state()).collections.find(c=>c.name==='Later collection');
     assert.ok(later);
     const edit=await page.request.get(base+'/task/edit/a',{headers});
     const fields=await page.evaluate(html=>{
      const form=new DOMParser().parseFromString(html,'text/html').querySelector('form[method="post"]');
      return Object.fromEntries(Array.from(form.querySelectorAll('input[name],select[name],textarea[name]')).filter(el=>!['checkbox','radio'].includes(el.type)||el.checked).map(el=>[el.name,el.value]));
     },await edit.text());
     fields.collection_id=later.id;
     assert.equal((await page.request.post(base+'/task/edit/a',{headers,form:fields})).status(),200);
     assert.equal((await readTask()).collection_id,later.id);
     await clear(409);
     await page.getByText('任务已加入其他合集，未移出，请刷新后重新选择',{exact:true}).first().waitFor();
     assert.equal((await readTask()).collection_id,later.id);
     await notice().getByRole('button',{name:'重试未完成项',exact:true}).click();
     await settled();
     assert.equal((await readTask()).collection_id,later.id);
     assert.deepEqual(errors,[]);
     assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
     console.log(JSON.stringify({path,viewport,clearCollection:true,status:'passed'}));
     await context.close();
     continue;
    }
    if(process.env.FLS_TOGGLE_LOG_ONLY){
     await page.goto(base+'/log/a?back=/collections');
     const toggle=()=>page.locator('form[action^="/task/toggle/a"]');
     const old=await toggle().evaluate(form=>({action:form.getAttribute('action'),fields:Object.fromEntries(new FormData(form))}));
     assert.equal(old.fields.enabled,'0');
     assert.match(old.fields.request_id,/^[a-f0-9]{32}$/);
     await fixture({save:true});
     let response=page.waitForResponse(r=>r.request().method()==='POST' && new URL(r.url()).pathname==='/task/toggle/a');
     await toggle().getByRole('button',{name:'停用任务',exact:true}).click();
     assert.equal((await response).status(),500);
     assert.equal((await state()).tasks.find(t=>t.id==='a').enabled,true);
     await fixture({reload:true});
     response=page.waitForResponse(r=>r.request().method()==='POST' && new URL(r.url()).pathname==='/task/toggle/a');
     await page.getByRole('button',{name:'重试保存状态',exact:true}).click();
     assert.equal((await response).status(),503);
     assert.equal((await state()).tasks.find(t=>t.id==='a').enabled,false);
     await fixture({});
     await page.getByRole('button',{name:'重新加载调度器',exact:true}).click();
     await page.waitForURL(url=>url.pathname==='/log/a' && url.searchParams.get('back')==='/collections');
     assert.equal(await toggle().locator('input[name="enabled"]').inputValue(),'1');
     await toggle().getByRole('button',{name:'启用任务',exact:true}).click();
     await toggle().getByRole('button',{name:'停用任务',exact:true}).waitFor();
     assert.equal((await state()).tasks.find(t=>t.id==='a').enabled,true);
     await page.route('**/task/toggle/a?*',async route=>{
      await route.fetch();
      return route.abort();
     });
     await toggle().getByRole('button',{name:'停用任务',exact:true}).click();
     await page.getByText(/请求失败：/).waitFor();
     assert.equal((await state()).tasks.find(t=>t.id==='a').enabled,false);
     await page.unroute('**/task/toggle/a?*');
     await toggle().getByRole('button',{name:'停用任务',exact:true}).click();
     await toggle().getByRole('button',{name:'启用任务',exact:true}).waitFor();
     assert.equal((await state()).tasks.find(t=>t.id==='a').enabled,false);
     assert.deepEqual(errors,[]);
     assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
     console.log(JSON.stringify({path,viewport,toggleLog:true,status:'passed'}));
     await context.close();
     continue;
    }
    if(process.env.FLS_EXECUTION_FORM_ERRORS_ONLY){
     await fixture({reset:true,config:true});
     await page.goto(base+'/task/config/a');
     const before=(await state()).history.length;
     const match=url=>url.pathname==='/run/a';
     await page.route(match,route=>route.fulfill({status:502,contentType:'text/plain',body:'fixture form unavailable'}));
     let response=page.waitForResponse(r=>r.request().method()==='POST' && new URL(r.url()).pathname==='/run/a');
     await page.locator('button[formaction^="/run/a"]').click();
     assert.equal((await response).status(),502);
     const notice=()=>page.locator('[data-bulk-recovery="task:a"]');
     await notice().getByRole('button',{name:'查询本次操作结果',exact:true}).waitFor();
     assert.equal((await state()).history.length,before);
     const pending=await page.evaluate(()=>flsBulkState('task:a').pending.request_id);
     await page.unroute(match);
     await page.reload();
     await page.locator('button[formaction^="/run/a"]').click();
     await notice().getByText('上次请求尚未确认，请先查询本次操作结果',{exact:true}).waitFor();
     assert.equal((await state()).history.length,before);
     assert.equal(await page.evaluate(()=>flsBulkState('task:a').pending.request_id),pending);
     await page.goto(base+'/logout');
     await login();
     await page.goto(base+'/task/config/a');
     await notice().getByRole('button',{name:'查询本次操作结果',exact:true}).click();
     await settled();
     for(let i=0;i<100 && (await state()).history[0]?.status!=='success';i++) await page.waitForTimeout(30);
     assert.equal((await state()).history.length,before+1);
     assert.equal((await state()).history[0].status,'success');
     assert.deepEqual(errors,[]);
     console.log(JSON.stringify({path,viewport,executionFormErrors:true,status:'passed'}));
     await context.close();
     continue;
    }
    if(process.env.FLS_EXECUTION_EXTRA_ONLY){
     await fixture({reset:true,config:true});
     await page.goto(base+'/task/config/a');
     const original=await page.locator('textarea[name="content"]').inputValue();
     await page.locator('textarea[name="content"]').fill('unsaved draft');
     let response=page.waitForResponse(r=>r.request().method()==='POST' && new URL(r.url()).pathname==='/run/a');
     await page.locator('button[formaction^="/run/a"]').click();
     const submitted=await response;
     assert.equal(submitted.status(),302);
     assert.ok(submitted.request().postData().includes('name="request_id"'));
     await page.waitForURL(url=>url.pathname==='/log/a');
     await page.goto(base+'/task/config/a');
     assert.equal(await page.locator('textarea[name="content"]').inputValue(),original);
     let dropped=false;
     const routeFor=url=>url.pathname==='/run/a';
     await page.route(routeFor,async route=>{
      if(!dropped){dropped=true;await route.fetch();return route.abort();}
      return route.continue();
     });
     await page.locator('button[formaction^="/run/a"]').click();
     const notice=()=>page.locator('[data-bulk-recovery="task:a"]');
     await notice().getByRole('button',{name:'查询本次操作结果',exact:true}).waitFor();
     const before=(await state()).history.length;
     await page.reload();
     await notice().getByRole('button',{name:'查询本次操作结果',exact:true}).click();
     await settled();
     assert.equal((await state()).history.length,before);
     assert.equal(await page.locator('textarea[name="content"]').inputValue(),original);
     await page.unroute(routeFor);
     await page.goto(base+'/tasks');
     const single=()=>page.locator('[onclick*="taskAjaxAction(\'run\',\'a\'"]:visible').first();
     let rejected=false;
     await page.route('**/api/task/action/run/a',route=>{
      if(!rejected){rejected=true;return route.fulfill({status:502,contentType:'text/plain',body:'fixture unavailable'});}
      return route.continue();
     });
     await single().click();
     await notice().getByRole('button',{name:'查询本次操作结果',exact:true}).waitFor();
     await settled();
     assert.equal((await state()).history.length,before);
     await notice().getByRole('button',{name:'查询本次操作结果',exact:true}).click();
     await settled();
     for(let i=0;i<100 && (await state()).history[0].status!=='success';i++) await page.waitForTimeout(30);
     assert.equal((await state()).history.length,before+1);
     await page.unroute('**/api/task/action/run/a');
     await fixture({unknown:true});
     await single().click();
     await notice().getByRole('button',{name:'确认后重试本项',exact:true}).waitFor();
     await settled();
     for(let i=0;i<100 && (await state()).history[0].status!=='success';i++) await page.waitForTimeout(30);
     assert.equal((await state()).history.length,before+2);
     await fixture({});
     await notice().getByRole('button',{name:'确认后重试本项',exact:true}).click();
     await settled();
     for(let i=0;i<100 && (await state()).history[0].status!=='success';i++) await page.waitForTimeout(30);
     assert.equal((await state()).history.length,before+3);
     assert.deepEqual(errors,[]);
     console.log(JSON.stringify({path,viewport,executionExtra:true,status:'passed'}));
     await context.close();
     continue;
    }
    if(process.env.FLS_EXECUTION_ONLY){
     const requestId=()=>require('node:crypto').randomBytes(16).toString('hex');
     const history=async status=>{
      for(let i=0;i<100;i++){
       const current=await state();
       if(current.history[0] && current.history[0].status===status) return current;
       await page.waitForTimeout(30);
      }
      throw new Error('history did not become '+status);
     };
     const count=async ()=>(await state()).history.length;
     const single=action=>page.locator('[onclick*="taskAjaxAction(\''+action+'\',\'a\'"]:visible').first();
     const submit=action=>path==='/tasks'?single(action):page.locator('form[action^="/'+action+'/a"] button:visible').first();
     const endpoint=action=>path==='/tasks'?'/api/task/action/'+action+'/a':'/'+action+'/a';
     const responseFor=action=>page.waitForResponse(r=>r.request().method()==='POST' && new URL(r.url()).pathname===endpoint(action));
     const notice=()=>page.locator('[data-bulk-recovery="task:a"]');
     const initialCount=await count();
     await fixture({start_fail:true});
     let response=responseFor('run');
     await submit('run').click();
     assert.equal((await response).status(),500);
     await notice().waitFor();
     assert.equal(await count(),initialCount);
     await fixture({});
     if(path==='/tasks'){
      const csrf=await page.locator('meta[name="csrf-token"]').getAttribute('content');
      await page.locator('meta[name="csrf-token"]').evaluate(el=>el.content='invalid');
      response=responseFor('run');
      await notice().getByRole('button',{name:'重试未完成项',exact:true}).click();
      assert.equal((await response).status(),400);
      await settled();
      assert.equal(await count(),initialCount);
      await page.locator('meta[name="csrf-token"]').evaluate((el,token)=>el.content=token,csrf);
      response=responseFor('run');
      await notice().getByRole('button',{name:'查询本次操作结果',exact:true}).click();
      assert.equal((await response).status(),200);
      await settled();
     }else{
      await page.getByRole('button',{name:'重试运行',exact:true}).click();
      await page.waitForURL(url=>url.pathname==='/log/a');
     }
     await history('success');
     const before=await count();
     if(path==='/tasks') await page.goto(base+path);
     else await page.reload();
     const runRoute=url=>url.pathname===endpoint('run');
     let dropped=false;
     await page.route(runRoute,async route=>{
      if(!dropped){dropped=true;await route.fetch();return route.abort();}
      return route.continue();
     });
     await submit('run').click();
     await notice().getByRole('button',{name:'查询本次操作结果',exact:true}).waitFor();
     await history('success');
     assert.equal(await count(),before+1);
     if(path==='/tasks') await settled();
     await page.reload();
     await notice().getByRole('button',{name:'查询本次操作结果',exact:true}).click();
     await settled();
     assert.equal(await count(),before+1);
     assert.equal(await notice().getByRole('link',{name:'查看任务日志',exact:true}).getAttribute('href'),'/log/a');
     await page.unroute(runRoute);
     await fixture({reset:true,long:true,collection:path==='/collections'});
     assert.equal((await page.request.post(base+'/api/task/action/run/a',{headers,data:{request_id:requestId()}})).status(),200);
     await history('running');
     await page.goto(base+path);
     const stopRoute=url=>url.pathname===endpoint('stop');
     dropped=false;
     await page.route(stopRoute,async route=>{
      if(!dropped){dropped=true;await route.fetch();return route.abort();}
      return route.continue();
     });
     await submit('stop').click();
     await notice().getByRole('button',{name:'查询本次操作结果',exact:true}).waitFor();
     await history('stopped');
     if(path==='/tasks') await settled();
     assert.equal((await page.request.post(base+'/api/task/action/run/a',{headers,data:{request_id:requestId()}})).status(),200);
     await history('running');
     await page.reload();
     await notice().getByRole('button',{name:'查询本次操作结果',exact:true}).click();
     await settled();
     assert.equal((await state()).history[0].status,'running');
     await notice().getByRole('link',{name:'查看任务日志',exact:true}).scrollIntoViewIfNeeded();
     if(process.env.FLS_BULK_SCREENSHOTS) await page.screenshot({path:process.env.FLS_BULK_SCREENSHOTS+'/execution-'+path.slice(1)+'-'+viewport.width+'.png'});
     assert.equal((await page.request.post(base+'/api/task/action/stop/a',{headers,data:{request_id:requestId()}})).status(),200);
     await history('stopped');
     await page.unroute(stopRoute);
     assert.deepEqual(errors,[]);
     assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
     console.log(JSON.stringify({path,viewport,execution:true,status:'passed'}));
     await context.close();
     continue;
    }
    if(process.env.FLS_COPY_DELETE_ONLY){
     const single=action=>page.locator('[onclick*="taskAjaxAction(\''+action+'\',\'a\'"]:visible').first();
     const responseFor=action=>page.waitForResponse(r=>r.request().method()==='POST' && new URL(r.url()).pathname==='/api/task/action/'+action+'/a');
     if(path==='/tasks'){
      const copy=async status=>{
       await page.locator('[data-task-id="a"] .task-action-menu-toggle:visible').first().click();
       const response=responseFor('copy');
       await single('copy').click();
       const result=await response;
       assert.equal(result.status(),status);
       await settled();
       return result.json();
      };
      await fixture({save:true});
      await copy(500);
      assert.equal((await state()).tasks.length,2);
      await fixture({reload:true});
      let response=responseFor('copy');
      await page.getByRole('button',{name:'重试未完成项',exact:true}).click();
      const saved=await (await response).json();
      await settled();
      assert.equal(saved.saved,true);
      assert.equal((await state()).tasks.length,3);
      assert.equal(await page.getByRole('link',{name:'查看已保存副本',exact:true}).getAttribute('href'),'/task/edit/'+saved.created_ids[0]+'?back=%2Ftasks');
      await page.getByRole('link',{name:'查看已保存副本',exact:true}).click();
      await page.locator('input[name="name"]').waitFor();
      assert.equal(await page.locator('input[name="name"]').inputValue(),'Bulk a-copy');
      await page.goto(base+path);
      await page.getByRole('button',{name:'重新加载调度器',exact:true}).click();
      await page.getByText(/调度器重载失败/).waitFor();
      await fixture({});
      await page.getByRole('button',{name:'重新加载调度器',exact:true}).click();
      await page.getByText('已重新加载调度器',{exact:true}).waitFor();
      assert.equal((await state()).tasks.length,3);
      let dropped=false;
      await page.route('**/api/task/action/copy/a',async route=>{
       if(!dropped){dropped=true;await route.fetch();return route.abort();}
       return route.continue();
      });
      await page.locator('[data-task-id="a"] .task-action-menu-toggle:visible').first().click();
      await single('copy').click();
      await page.getByRole('button',{name:'查询本次操作结果',exact:true}).waitFor();
      await settled();
      await page.reload();
      await page.getByRole('button',{name:'查询本次操作结果',exact:true}).click();
      await settled();
      assert.equal((await state()).tasks.length,4);
      await page.unroute('**/api/task/action/copy/a');
      await fixture({reload:true});
      dropped=false;
      await page.route('**/api/task/action/delete/a',async route=>{
       if(!dropped){dropped=true;await route.fetch();return route.abort();}
       return route.continue();
      });
      await single('delete').click();
      await page.getByRole('button',{name:'查询本次操作结果',exact:true}).waitFor();
      await settled();
      await page.reload();
      assert.equal(await page.locator('.task-select-checkbox[data-task-id="a"]').count(),0);
      await page.getByRole('button',{name:'查询本次操作结果',exact:true}).click();
      await settled();
      await page.getByRole('button',{name:'重新加载调度器',exact:true}).waitFor();
      assert.equal((await state()).tasks.find(t=>t.id==='a'),undefined);
      if(process.env.FLS_BULK_SCREENSHOTS) await page.screenshot({path:process.env.FLS_BULK_SCREENSHOTS+'/delete-recovery-'+viewport.width+'.png',fullPage:true});
      await fixture({});
      await page.getByRole('button',{name:'重新加载调度器',exact:true}).click();
      await page.getByText('已重新加载调度器',{exact:true}).waitFor();
      await page.unroute('**/api/task/action/delete/a');
      await fixture({reset:true,unknown:true});
      await page.goto(base+path);
      await single('delete').click();
      const unknownRetry=page.getByRole('button',{name:'确认后重试本项',exact:true});
      await unknownRetry.waitFor();
      await settled();
      assert.ok((await state()).tasks.find(t=>t.id==='a'));
      await fixture({});
      response=responseFor('delete');
      await unknownRetry.click();
      assert.equal((await response).status(),200);
      await settled();
      assert.equal((await state()).tasks.find(t=>t.id==='a'),undefined);
     }else{
      const form=page.locator('form[action^="/task/delete/a"]');
      await fixture({save:true});
      let response=page.waitForResponse(r=>r.request().method()==='POST' && new URL(r.url()).pathname==='/task/delete/a');
      await form.getByRole('button',{name:'删除任务',exact:true}).click();
      assert.equal((await response).status(),500);
      await page.locator('[data-task-delete-recovery]').waitFor();
      assert.ok((await state()).tasks.find(t=>t.id==='a'));
      await fixture({reload:true});
      response=page.waitForResponse(r=>r.request().method()==='POST' && new URL(r.url()).pathname==='/task/delete/a');
      await page.getByRole('button',{name:'重试删除',exact:true}).click();
      assert.equal((await response).status(),503);
      await page.getByRole('button',{name:'重新加载调度器',exact:true}).waitFor();
      assert.equal((await state()).tasks.find(t=>t.id==='a'),undefined);
      await fixture({});
      await page.getByRole('button',{name:'重新加载调度器',exact:true}).click();
      await page.waitForURL(url=>url.pathname==='/collections');
      assert.equal(await page.locator('form[action^="/task/delete/a"]').count(),0);
     }
     assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
     assert.deepEqual(errors,[]);
     console.log(JSON.stringify({path,viewport,copyDelete:true,status:'passed'}));
     await context.close();
     continue;
    }
    if(process.env.FLS_TOGGLE_ONLY){
     const toggle=()=>path==='/tasks'
      ? page.locator('[onclick*="taskAjaxAction(\'toggle\',\'a\'"]:visible').first()
      : page.locator('form[action^="/task/toggle/a"] button:visible');
     const clickToggle=async status=>{
      const response=page.waitForResponse(r=>r.request().method()==='POST' && new URL(r.url()).pathname===(path==='/tasks'?'/api/task/bulk-action':'/task/toggle/a'));
      await toggle().click();
      assert.equal((await response).status(),status);
      if(path==='/tasks') await settled();
     };
     await fixture({reload:true});
     await clickToggle(503);
     await page.getByRole('button',{name:'重新加载调度器',exact:true}).waitFor();
     assert.equal((await state()).tasks.find(t=>t.id==='a').enabled,false);
     await page.getByRole('button',{name:'重新加载调度器',exact:true}).click();
     await page.getByText(/调度器重载失败/).first().waitFor();
     await fixture({});
     const restored=page.waitForResponse(r=>r.request().method()==='POST' && new URL(r.url()).pathname===(path==='/tasks'?'/api/scheduler/reload':'/scheduler/reload'));
     await page.getByRole('button',{name:'重新加载调度器',exact:true}).click();
     assert.equal((await restored).status(),path==='/tasks'?200:302);
     await toggle().waitFor();
     assert.equal((await state()).tasks.find(t=>t.id==='a').enabled,false);
     await fixture({save:true});
     await clickToggle(500);
     const retry=page.getByRole('button',{name:path==='/tasks'?'重试未完成项':'重试保存状态',exact:true});
     await retry.waitFor();
     assert.equal((await state()).tasks.find(t=>t.id==='a').enabled,false);
     await fixture({});
     await retry.click();
     if(path==='/tasks') await settled();
     await toggle().waitFor();
     assert.equal((await state()).tasks.find(t=>t.id==='a').enabled,true);
     if(path==='/tasks'){
      let dropped=false;
      await page.route('**/api/task/bulk-action',async route=>{
       if(!dropped){ dropped=true; await route.fetch(); return route.abort(); }
       return route.continue();
      });
      await toggle().click();
      await page.getByRole('button',{name:'查询本次操作结果',exact:true}).waitFor();
      await settled();
      await page.reload();
      await page.getByRole('button',{name:'查询本次操作结果',exact:true}).click();
      await settled();
      assert.equal((await state()).tasks.find(t=>t.id==='a').enabled,false);
     }
     assert.deepEqual(errors,[]);
     console.log(JSON.stringify({path,viewport,toggle:true,status:'passed'}));
     await context.close();
     continue;
    }
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
