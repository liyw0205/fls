"""Real HTTP/Chromium checks for recoverable backup creation."""
import os
import subprocess
import tempfile
import threading

from tools.browser_regression import seed_browser_fixture, TOKEN, find_playwright_core, find_chromium


def main():
    playwright, chromium = find_playwright_core(), find_chromium()
    if not playwright or not chromium:
        raise SystemExit("Set FLS_PLAYWRIGHT_CORE and FLS_CHROMIUM")
    with tempfile.TemporaryDirectory(prefix="fls-backup-browser-") as temp:
        seed_browser_fixture(temp)
        os.environ.update(FLS_BASE_DIR=temp, FLS_TOKEN=TOKEN, FLS_SECRET_KEY="backup-browser-secret")
        from flask import jsonify, request
        from werkzeug.serving import make_server
        from fls_manager.app import create_app
        import fls_manager.app as app_module
        from fls_manager import scheduler
        from fls_manager.routes.backup import _common as backup, actions

        app = create_app()
        app_module.dispatch_frontend_open_event = lambda: None
        fault, workers = {}, []
        release = threading.Event()
        release.set()
        persist, receipt, worker = backup.persist_backup_job, actions.write_json, backup._create_backup_worker

        def save(info):
            if fault.get("job") and info["status"] == "准备中":
                raise OSError("fixture job storage failure")
            return persist(info)

        def write(path, record):
            if fault.get("receipt"):
                receipt(path, record)
                raise OSError("fixture receipt confirmation failure")
            return receipt(path, record)

        def run(*args):
            workers.append(args[0])
            release.wait(20)
            worker(*args)

        backup.persist_backup_job, actions.write_json = save, write
        backup._create_backup_worker = run
        backup.write_dependencies_file = lambda *args: None

        @app.route("/api/fixture/backup", methods=["GET", "POST"])
        def fixture():
            if request.method == "POST":
                fault.clear()
                fault.update(request.get_json())
                if fault.get("block"):
                    release.clear()
                else:
                    release.set()
            return jsonify(workers=workers, jobs=list(backup.BACKUP_JOBS.values()), active=backup.ACTIVE_BACKUP_WORKERS)

        server = make_server("127.0.0.1", 0, app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        env = dict(os.environ, FLS_UI_BASE=f"http://127.0.0.1:{server.server_port}", FLS_UI_TOKEN=TOKEN,
                   FLS_PLAYWRIGHT_CORE=str(playwright), FLS_CHROMIUM=str(chromium))
        try:
            result = subprocess.run(["node", "-e", SCRIPT], env=env, timeout=180)
            if result.returncode:
                raise SystemExit(result.returncode)
        finally:
            release.set()
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
   await page.addInitScript(()=>Object.defineProperty(crypto,'randomUUID',{value:undefined}));
   page.on('dialog',d=>d.accept());
   const errors=[];page.on('pageerror',e=>errors.push(e.message));
   const base=process.env.FLS_UI_BASE,headers={'X-Token':process.env.FLS_UI_TOKEN};
   const state=async()=>(await page.request.get(base+'/api/fixture/backup',{headers})).json();
   const fixture=async data=>assert.equal((await page.request.post(base+'/api/fixture/backup',{headers,data})).status(),200);
   const login=async()=>{
    await page.locator('input[name="token"]').fill(process.env.FLS_UI_TOKEN);
    await page.locator('button[type="submit"]').click();await page.waitForURL(u=>u.pathname!='/login');
   };
   const create=()=>page.locator('#backupCreateForm button[type="button"]');
   const query=()=>page.getByRole('button',{name:'查询原备份请求',exact:true});
   const progress=()=>page.locator('#backupJobText');
   const submit=async button=>{
    const response=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/backup/create' && r.request().method()==='POST');
    await button.click();return response;
   };
   const settled=async()=>{
    await page.waitForFunction(()=>!sessionStorage.getItem('fls-backup-request'));
    await page.getByRole('link',{name:'下载备份',exact:true}).first().waitFor();
   };
   await page.goto(base+'/login');await login();await page.goto(base+'/backup');
   const initial=(await state()).workers.length;
   const fields=await page.locator('#backupCreateForm').evaluate(f=>({request_id:f.elements.request_id.value,items:['scripts']}));
   assert.equal((await page.request.post(base+'/api/backup/create',{data:fields,headers:{'X-CSRF-Token':'bad'}})).status(),400);
   const csrf=await page.locator('meta[name="csrf-token"]').getAttribute('content');
   assert.equal((await page.request.post(base+'/api/backup/create',{data:{...fields,items:['scripts','bad']},headers:{'X-CSRF-Token':csrf}})).status(),400);
   assert.equal((await state()).workers.length,initial);
   await page.locator('#backupCreateForm [value="data"]').uncheck();
   // Real creation completes, but its HTTP response never reaches the page.
   await page.route('**/api/backup/create',async route=>{await route.fetch();await route.abort();},{times:1});
   await create().click();await query().waitFor();assert.equal((await state()).workers.length,initial+1);
   await page.reload();await query().waitFor();
   await page.locator('#backupCreateForm [value="data"]').check();
   assert.equal((await submit(query())).status(),200);await settled();
   assert.equal((await state()).workers.length,initial+1);
   const row=(await state()).jobs.find(row=>row.id===fields.request_id);
   assert.deepEqual(row.items,['scripts']);assert.equal(row.status,'已完成');
   assert.equal((await page.request.get(base+'/backup/download/'+row.filename)).status(),200);
   // A real disk failure preserves the request and retries the same target after repair.
   await fixture({job:true});assert.equal((await submit(create())).status(),500);await query().waitFor();
   const failed=await page.evaluate(()=>JSON.parse(sessionStorage.getItem('fls-backup-request')).request_id);
   await fixture({});assert.equal((await submit(query())).status(),200);await settled();
   assert.equal((await state()).jobs.find(row=>row.id===failed).status,'已完成');
   // A published prepared receipt needs explicit confirmation after refresh.
   await fixture({receipt:true});assert.equal((await submit(create())).status(),500);await query().waitFor();
   await page.reload();await fixture({});assert.equal((await submit(query())).status(),409);
   const confirm=page.getByRole('button',{name:'确认后继续原备份',exact:true});await confirm.waitFor();
   assert.equal((await submit(confirm)).status(),200);await settled();
   // A non-JSON gateway response survives navigation and a fresh login.
   await page.route('**/api/backup/create',r=>r.fulfill({status:502,contentType:'text/plain',body:'fixture gateway'}),{times:1});
   await create().click();await query().waitFor();
   await page.goto(base+'/logout');await login();await page.goto(base+'/backup');
   assert.equal((await submit(query())).status(),200);await settled();
   // Polling and cancellation losses keep a query action for the original running job.
   await fixture({block:true});await submit(create());
   const cancel=page.getByRole('button',{name:'取消备份',exact:true});await cancel.waitFor();
   await page.route('**/api/backup/cancel/*',async route=>{await route.fetch();await route.abort();},{times:1});
   await cancel.click();await query().waitFor();
   await fixture({});await query().click();
   await page.waitForFunction(()=>!sessionStorage.getItem('fls-backup-request'));
   assert.match(await progress().innerText(),/已取消/);
   // A poll failure after submission survives page reload and resumes without creating.
   await fixture({block:true});
   await page.route('**/api/backup/job/*',r=>r.fulfill({status:503,contentType:'text/plain',body:'fixture polling'}),{times:1});
   await submit(create());await query().waitFor();
   const before=(await state()).workers.length;
   await fixture({});await page.reload();await settled();assert.equal((await state()).workers.length,before);
   // Missing job records require explicit confirmation before a new request is allowed.
   await page.evaluate(()=>sessionStorage.setItem('fls-backup-request',JSON.stringify({job_id:'f'.repeat(32)})));
   await page.reload();
   await page.getByRole('button',{name:'核对后重新选择备份',exact:true}).click();
   assert.equal(await page.evaluate(()=>sessionStorage.getItem('fls-backup-request')),null);
   assert.equal(await create().isEnabled(),true);
   await page.evaluate(()=>{document.documentElement.style.scrollBehavior='auto';window.scrollTo(0,0);});
   if(process.env.FLS_BULK_SCREENSHOTS) await page.screenshot({path:process.env.FLS_BULK_SCREENSHOTS+'/backup-request-'+viewport.width+'.png',fullPage:true});
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
   assert.deepEqual(errors,[]);console.log('backup request recovery passed '+viewport.width);
   await context.close();
  }
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
'''


if __name__ == "__main__":
    main()
