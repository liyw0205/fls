"""Real HTTP/browser recovery for backup file deletion and download."""
import io
import os
import subprocess
import tarfile
import tempfile
import threading

from tools.browser_regression import TOKEN, find_chromium, find_playwright_core, seed_browser_fixture


def main():
    playwright, chromium = find_playwright_core(), find_chromium()
    if not playwright or not chromium:
        raise SystemExit("Set FLS_PLAYWRIGHT_CORE and FLS_CHROMIUM")
    with tempfile.TemporaryDirectory(prefix="fls-backup-files-browser-") as temp:
        seed_browser_fixture(temp)
        os.environ.update(FLS_BASE_DIR=temp, FLS_TOKEN=TOKEN, FLS_SECRET_KEY="backup-files-secret")
        from flask import jsonify, request
        from werkzeug.serving import make_server
        from fls_manager.app import create_app
        import fls_manager.app as app_module
        from fls_manager.routes.backup import _common as backup
        from fls_manager import scheduler

        app = create_app()
        app_module.dispatch_frontend_open_event = lambda: None
        fault, attempts = {}, []
        filename = "quoted'\"<archive>.tar.gz"
        target = backup.backup_dir() / filename
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as tar:
            entry = tarfile.TarInfo("scripts/example.py")
            data = b"print('browser archive')"
            entry.size = len(data)
            tar.addfile(entry, io.BytesIO(data))
        target.write_bytes(archive.getvalue())
        real_unlink = backup.os.unlink

        def unlink(name, *args, **kwargs):
            if name == filename and "dir_fd" in kwargs:
                attempts.append(name)
                if fault.get("deny"):
                    raise PermissionError("fixture delete denied")
            return real_unlink(name, *args, **kwargs)

        backup.os.unlink = unlink

        @app.route("/api/fixture/files", methods=["GET", "POST"])
        def fixture():
            if request.method == "POST":
                fault.clear()
                fault.update(request.get_json())
                if fault.get("replace"):
                    replacement = target.with_suffix(".replacement")
                    replacement.write_bytes(archive.getvalue())
                    replacement.replace(target)
                elif fault.get("seed"):
                    target.write_bytes(archive.getvalue())
            return jsonify(attempts=len(attempts), exists=target.exists(), filename=filename)

        server = make_server("127.0.0.1", 0, app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            env = dict(os.environ, FLS_UI_BASE=f"http://127.0.0.1:{server.server_port}",
                       FLS_UI_TOKEN=TOKEN, FLS_PLAYWRIGHT_CORE=str(playwright), FLS_CHROMIUM=str(chromium))
            subprocess.run(["node", "-e", SCRIPT], env=env, check=True, timeout=180)
        finally:
            backup.os.unlink = real_unlink
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
   const fixture=async data=>{const r=await page.request.post(base+'/api/fixture/files',{headers,data});assert.equal(r.status(),200);return r.json();};
   const state=async()=>(await page.request.get(base+'/api/fixture/files',{headers})).json();
   const login=async()=>{await page.goto(base+'/login');await page.locator('[name="token"]').fill(process.env.FLS_UI_TOKEN);await page.locator('button[type="submit"]').click();await page.waitForURL(u=>u.pathname!='/login');};
   await fixture({seed:true});await login();await page.goto(base+'/backup');
   const row=page.locator('#backupListTbody tr').first(),button=row.getByRole('button',{name:'删除备份',exact:true});
   const recovery=page.locator('#backupDeleteRecovery'),pendingRaw=()=>page.evaluate(()=>sessionStorage.getItem('fls-backup-delete'));
   const corrupt='{broken-json';await page.evaluate(v=>sessionStorage.setItem('fls-backup-delete',v),corrupt);await page.reload();
   await recovery.getByText('本地删除状态损坏',{exact:false}).waitFor();assert.equal(await pendingRaw(),corrupt);
   let response=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/backup/list');
   await page.route('**/api/backup/list',r=>r.fulfill({status:503,contentType:'text/html',body:'temporarily unavailable'}),{times:1});
   await recovery.getByRole('button',{name:'查询实际备份列表'}).click();assert.equal((await response).status(),503);assert.equal(await pendingRaw(),corrupt);
   await page.route('**/api/backup/list',r=>r.fulfill({status:200,contentType:'application/json',body:'{"ok":true,'}),{times:1});
   response=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/backup/list');
   await recovery.getByRole('button',{name:'查询实际备份列表'}).click();assert.equal((await response).status(),200);assert.equal(await pendingRaw(),corrupt);
   await page.route('**/api/backup/list',r=>r.fulfill({status:200,contentType:'application/json',body:'{"ok":true,"items":{}}'}),{times:1});
   response=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/backup/list');
   await recovery.getByRole('button',{name:'查询实际备份列表'}).click();assert.equal((await response).status(),200);assert.equal(await pendingRaw(),corrupt);
   const badShape=JSON.stringify({filename:'../replacement.tar.gz',version:'bad'});
   await page.evaluate(v=>sessionStorage.setItem('fls-backup-delete',v),badShape);await page.reload();
   await recovery.getByText('本地删除状态损坏',{exact:false}).waitFor();
   response=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/backup/list');
   await recovery.getByRole('button',{name:'查询实际备份列表'}).click();assert.equal((await response).status(),200);
   await recovery.getByText('真实备份列表已刷新',{exact:false}).waitFor();assert.equal(await pendingRaw(),badShape);
   await recovery.getByRole('button',{name:'核对后清理损坏状态'}).click();
   await page.waitForFunction(()=>sessionStorage.getItem('fls-backup-delete')===null);
   assert.equal(await button.isEnabled(),true);
   const initial=await state(),initialVersion=await button.getAttribute('data-version');assert.equal(await button.getAttribute('data-filename'),initial.filename);
   const download=await page.request.get(base+await row.getByRole('link',{name:'下载备份'}).getAttribute('href'));
   assert.equal(download.status(),200);assert.equal((await download.body()).subarray(0,2).toString('hex'),'1f8b');
   await fixture({replace:true});
   response=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/backup/delete');await button.click();
   assert.equal((await response).status(),409);
   await recovery.getByText('备份文件已变化',{exact:false}).waitFor();
   assert.equal((await state()).attempts,initial.attempts);assert.equal(await button.isEnabled(),false);
   await recovery.getByRole('button',{name:'查询实际备份列表'}).click();
   await recovery.getByText('同名备份版本已变化',{exact:false}).waitFor();
   assert.equal(JSON.parse(await pendingRaw()).version,initialVersion);
   await recovery.getByRole('button',{name:'确认清理并重新选择'}).click();
   await page.waitForFunction(()=>sessionStorage.getItem('fls-backup-delete')===null);
   assert.equal(await button.isEnabled(),true);
   await fixture({deny:true});response=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/backup/delete');
   await button.click();assert.equal((await response).status(),500);
   await recovery.getByText('fixture delete denied',{exact:false}).waitFor();assert.equal(await button.isEnabled(),false);
   await recovery.getByRole('button',{name:'查询实际备份列表'}).click();
   await recovery.getByText('原备份仍存在且版本一致',{exact:false}).waitFor();
   const retained=JSON.parse(await pendingRaw());assert.equal(retained.version,await button.getAttribute('data-version'));
   await fixture({});response=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/backup/delete');
   await recovery.getByRole('button',{name:'重试删除原备份'}).click();assert.equal((await response).status(),200);
   assert.equal((await state()).exists,false);await fixture({seed:true});await page.reload();
   await page.route('**/api/backup/delete',r=>r.fulfill({status:502,contentType:'text/html',body:'gateway unavailable'}),{times:1});
   await button.click();await recovery.getByText('删除结果未确认',{exact:false}).waitFor();
   assert.equal((await state()).exists,true);await page.reload();
   await recovery.getByText('上次删除结果未确认',{exact:false}).waitFor();
   await recovery.getByRole('button',{name:'查询实际备份列表'}).click();
   await recovery.getByText('原备份仍存在且版本一致',{exact:false}).waitFor();
   await context.clearCookies();
   response=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/backup/delete');
   await recovery.getByRole('button',{name:'重试删除原备份'}).click();assert.equal((await response).status(),400);
   assert.equal((await state()).exists,true);
   await login();await page.goto(base+'/backup');
   await recovery.getByText('上次删除结果未确认',{exact:false}).waitFor();
   await recovery.getByRole('button',{name:'查询实际备份列表'}).click();
   await recovery.getByText('原备份仍存在且版本一致',{exact:false}).waitFor();
   const before=await state();
   await page.route('**/api/backup/delete',async r=>{const result=await r.fetch();assert.equal(result.status(),200);await r.abort('failed');},{times:1});
   await recovery.getByRole('button',{name:'重试删除原备份'}).click();await recovery.getByText('删除结果未确认',{exact:false}).waitFor();
   assert.equal((await state()).exists,false);assert.equal((await state()).attempts,before.attempts+1);
   await page.reload();await recovery.getByText('上次删除结果未确认',{exact:false}).waitFor();
   await recovery.getByRole('button',{name:'查询实际备份列表'}).click();
   await recovery.getByText('原备份已不存在',{exact:false}).waitFor();
   await recovery.getByRole('button',{name:'确认清理并重新选择'}).click();
   await page.waitForFunction(()=>sessionStorage.getItem('fls-backup-delete')===null);
   assert.equal(await page.locator('#backupListTbody').getByRole('button',{name:'删除备份'}).count(),0);
   assert.equal((await state()).attempts,before.attempts+1);
   assert.equal(await page.evaluate(()=>sessionStorage.getItem('fls-backup-delete')),null);
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
   await page.screenshot({path:'/tmp/fls-backup-files-'+viewport.width+'.png',fullPage:true});
   assert.deepEqual(errors,[]);await fixture({seed:true});
   const plain=await browser.newContext({viewport,javaScriptEnabled:false}),formPage=await plain.newPage();
   await formPage.goto(base+'/login');await formPage.locator('[name="token"]').fill(process.env.FLS_UI_TOKEN);
   await formPage.locator('button[type="submit"]').click();await formPage.waitForURL(u=>u.pathname!='/login');
   await formPage.goto(base+'/backup');const form=formPage.locator('form[action="/backup/delete"]');
   assert.ok(await form.locator('[name="csrf_token"]').inputValue());
   await Promise.all([formPage.waitForURL('**/backup/delete'),form.getByRole('button',{name:'确认删除备份'}).click()]);
   await formPage.getByRole('link',{name:'查询实际备份列表'}).click();await formPage.waitForURL('**/backup#backup-list');
   assert.equal((await state()).exists,false);await plain.close();await context.close();
   console.log('backup file recovery passed '+viewport.width);
  }
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
'''


if __name__ == "__main__":
    main()
