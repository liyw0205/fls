"""Real HTTP/Chromium checks for recoverable backup creation and restore."""
import os
import base64
import hashlib
import io
import json
import tarfile
import subprocess
import tempfile
import threading
from pathlib import Path

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
        from fls_manager import models
        from fls_manager.config import load_config
        from fls_manager.paths import SCRIPT_DIR
        from fls_manager.routes.backup import _common as backup, actions
        from fls_manager.routes.backup import restore
        from tests.test_bulk_workflows import sample_task

        app = create_app()
        app_module.dispatch_frontend_open_event = lambda: None
        fault, workers = {}, []
        release = threading.Event()
        release.set()
        persist, receipt, worker = backup.persist_backup_job, actions.write_json, backup._create_backup_worker
        write_archive_index = restore._write_restore_archive_index_file
        sync_restore_archive_directory = restore._sync_restore_archive_directory
        ack_sync_failure = {"enabled": False, "count": 0}
        extracted_limit = backup.MAX_BACKUP_EXTRACTED_BYTES
        reload, commit = scheduler._reload_scheduler_locked, restore._commit_restore_directories
        restore_receipt = restore._save_restore_request
        restores = []
        archive_paused, archive_release = threading.Event(), threading.Event()
        archive_release.set()
        add_archive, publish_archive = tarfile.TarFile.add, backup._publish_backup_archive

        def add_to_archive(archive, path, *args, **kwargs):
            result = add_archive(archive, path, *args, **kwargs)
            if fault.get("atomic_block") and str(path).endswith(".py"):
                archive_paused.set()
                if not archive_release.wait(20):
                    raise RuntimeError("fixture archive release timed out")
            if fault.get("atomic_write"):
                raise OSError("fixture archive compression fault")
            return result

        def publish_atomic(staged, target):
            mode = fault.get("atomic_publish")
            if not mode:
                return publish_archive(staged, target)
            link, lstat = backup.os.link, Path.lstat
            attempted = False

            def replace_with_fault(source, destination, *args, **kwargs):
                nonlocal attempted
                if Path(source) == staged:
                    if mode != "before":
                        link(source, destination, *args, **kwargs)
                    attempted = True
                    raise OSError("fixture archive publication fault")
                return link(source, destination, *args, **kwargs)

            def verify(path, *args, **kwargs):
                if mode == "unknown" and attempted and path == target:
                    raise PermissionError("fixture archive confirmation unavailable")
                return lstat(path, *args, **kwargs)

            backup.os.link, Path.lstat = replace_with_fault, verify
            try:
                return publish_archive(staged, target)
            finally:
                backup.os.link, Path.lstat = link, lstat

        tarfile.TarFile.add, backup._publish_backup_archive = add_to_archive, publish_atomic

        def apply_scheduler():
            if fault.get("reload"):
                scheduler._record_scheduler_state("failed", error="fixture reload unavailable")
                raise RuntimeError("fixture reload unavailable")
            return reload()

        def restore_directories(entries):
            restores.append(True)
            if fault.get("rollback"):
                real_replace = restore.os.replace

                def fail_install_and_rollback(source, destination):
                    source, destination = Path(source), Path(destination)
                    if source.name == "replacement" and destination == SCRIPT_DIR:
                        raise OSError("fixture script replacement failure")
                    if source.name == "previous" and destination == SCRIPT_DIR:
                        raise OSError("fixture script rollback failure")
                    return real_replace(source, destination)

                restore.os.replace = fail_install_and_rollback
                try:
                    return commit(entries)
                finally:
                    restore.os.replace = real_replace
            return commit(entries)

        scheduler._reload_scheduler_locked = apply_scheduler
        restore._commit_restore_directories = restore_directories

        def persist_restore(record):
            if fault.get("restore_receipt") and record["state"] == "received":
                restore_receipt(record)
                raise OSError("fixture received receipt failure")
            return restore_receipt(record)

        restore._save_restore_request = persist_restore

        def sync_archive_directory():
            if ack_sync_failure["enabled"]:
                state_path = restore._restore_archive_maintenance_path()
                try:
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    state = {}
                if state.get("acknowledged"):
                    ack_sync_failure["count"] += 1
                    if ack_sync_failure["count"] <= 2:
                        raise OSError("fixture acknowledgement directory fsync failure")
                    ack_sync_failure["enabled"] = False
            return sync_restore_archive_directory()

        restore._sync_restore_archive_directory = sync_archive_directory
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as tar:
            for name, data in {
                "data/config.json": json.dumps(load_config()).encode(),
                "data/tasks.json": json.dumps([sample_task("restored", command="echo restored", cron="0 0 1 1 *")]).encode(),
                "scripts/demo.py": b"print('restored')",
            }.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))

        def save(info):
            if fault.get("job") and info["status"] == "准备中":
                raise OSError("fixture job storage failure")
            if fault.get("cancel") and info["status"] == "正在取消":
                if fault["cancel"] == "after":
                    persist(info)
                raise OSError("fixture cancel storage failure")
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
                if fault.get("ack_directory_fsync_failure"):
                    ack_sync_failure.update(enabled=True, count=0)
                if fault.get("reset_restore_history"):
                    with backup.BACKUP_RESTORE_REQUEST_LOCK:
                        for path in restore.BACKUP_RESTORE_REQUESTS_DIR.glob("*.json"):
                            path.unlink(missing_ok=True)
                        (backup.BACKUP_RESTORE_REQUESTS_DIR / ".archive").unlink(missing_ok=True)
                        (backup.BACKUP_RESTORE_REQUESTS_DIR / ".archive.bak").unlink(missing_ok=True)
                if fault.get("seed_archive_candidate"):
                    request_id = "0" * 32
                    record = {
                        "request_id": request_id,
                        "archive_sha256": fault["archive_sha256"],
                        "filename": "older-restore.tar.gz",
                        "restore_items": ["data", "scripts"],
                        "restore_deps": False,
                        "state": "restored",
                        "scheduler_reload_required": False,
                        "scheduler_reloaded": True,
                        "dependencies_failed": False,
                        "created_at": "2000-01-01 00:00:00",
                        "updated_at": "2000-01-01 00:00:00",
                    }
                    backup.write_json(restore._restore_request_path(request_id), record)
                    restore.RESTORE_RECEIPT_DETAIL_LIMIT = 1
                if fault.get("corrupt_archive_index"):
                    mode = fault["corrupt_archive_index"]
                    paths = ([restore._restore_archive_index_path(), restore._restore_archive_backup_path()]
                             if mode == "both" else [restore._restore_archive_backup_path() if mode == "backup"
                                                      else restore._restore_archive_index_path()])
                    for path in paths:
                        path.write_text("{broken index", encoding="utf-8")
                if fault.get("fail_index_repair"):
                    def fail_index_repair(path, index):
                        if path in (restore._restore_archive_index_path(), restore._restore_archive_backup_path()):
                            raise OSError("fixture archive index repair unavailable")
                        return write_archive_index(path, index)
                    restore._write_restore_archive_index_file = fail_index_repair
                else:
                    restore._write_restore_archive_index_file = write_archive_index
                if fault.get("atomic_block"):
                    archive_paused.clear()
                    archive_release.clear()
                else:
                    archive_release.set()
                backup.MAX_BACKUP_EXTRACTED_BYTES = (
                    1 if fault.get("archive_limit") else extracted_limit
                )
                if fault.get("rollback"):
                    SCRIPT_DIR.mkdir(parents=True, exist_ok=True)
                    (SCRIPT_DIR / "rollback-original.py").write_text(
                        "print('original')", encoding="utf-8"
                    )
                if fault.get("block"):
                    release.clear()
                else:
                    release.set()
                if fault.get("edit"):
                    tasks = models.load_tasks()
                    tasks[0]["name"] = "Later edit"
                    models.save_tasks(tasks)
                    (SCRIPT_DIR / "demo.py").write_text("print('later edit')")
                if fault.get("unknown"):
                    record = restore._read_restore_request(fault["unknown"])
                    record.update(state="committing", scheduler_reloaded=False, scheduler_reload_required=True)
                    restore_receipt(record)
                if fault.get("retention"):
                    backup.BACKUP_JOBS.clear()
                    backup.BACKUP_CANCEL_EVENTS.clear()
                    backup.BACKUP_JOBS_FILE.unlink(missing_ok=True)

                    def retained_job(job_id, running, status, updated_at):
                        return {
                            "id": job_id,
                            "items": ["scripts"],
                            "type_text": "仅脚本",
                            "running": running,
                            "status": status,
                            "filename": "",
                            "size": 0,
                            "size_text": "-",
                            "error": "",
                            "created_at": "2020-01-01 00:00:00",
                            "updated_at": updated_at,
                        }

                    backup.persist_backup_job(retained_job("a" * 32, True, "正在压缩", "2020-01-01 00:00:00"))
                    for index in range(backup.BACKUP_JOB_LIMIT + 1):
                        backup.persist_backup_job(retained_job(
                            f"retention-terminal-{index:03d}", False, "已完成", f"2026-01-{index + 1:03d} 00:00:00"
                        ))
                    backup.BACKUP_JOBS.clear()
                    backup.BACKUP_CANCEL_EVENTS.clear()
                    backup.ACTIVE_BACKUP_WORKERS = 0
                if fault.get("orphan_stage"):
                    orphan_id = "c" * 32
                    orphan = {
                        "id": orphan_id,
                        "items": ["scripts"],
                        "type_text": "仅脚本",
                        "running": True,
                        "status": "正在压缩",
                        "filename": "",
                        "size": 0,
                        "size_text": "-",
                        "error": "",
                        "created_at": "2026-11-18 00:00:00",
                        "updated_at": "2026-11-18 00:00:00",
                        "staged_cleanup_pending": True,
                        "staged_cleanup_error": "",
                    }
                    backup.BACKUP_JOBS[orphan_id] = orphan
                    backup.persist_backup_job(orphan)
                    (backup.backup_dir() / backup._backup_staging_name(orphan_id)).write_bytes(b"partial archive")
                    backup.BACKUP_JOBS.pop(orphan_id, None)
                    fault["orphan_job_id"] = orphan_id
            return jsonify(workers=workers, jobs=list(backup.BACKUP_JOBS.values()), active=backup.ACTIVE_BACKUP_WORKERS,
                           archive_paused=archive_paused.is_set(), partials=[item.name for item in backup.BACKUP_DIR.glob("*.part")],
                           orphan_job_id=fault.get("orphan_job_id", ""),
                           restores=len(restores), tasks=models.load_tasks(), scheduled=[job.id for job in scheduler.scheduler.get_jobs()],
                           script=(SCRIPT_DIR / "demo.py").read_text() if (SCRIPT_DIR / "demo.py").exists() else "")

        server = make_server("127.0.0.1", 0, app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        env = dict(os.environ, FLS_UI_BASE=f"http://127.0.0.1:{server.server_port}", FLS_UI_TOKEN=TOKEN,
                   FLS_PLAYWRIGHT_CORE=str(playwright), FLS_CHROMIUM=str(chromium), FLS_RESTORE_ARCHIVE=base64.b64encode(archive.getvalue()).decode())
        try:
            script = ARCHIVE_INDEX_SCRIPT if os.environ.get("FLS_BACKUP_ARCHIVE_INDEX") else ATOMIC_SCRIPT if os.environ.get("FLS_BACKUP_ATOMIC") else ROLLBACK_SCRIPT if os.environ.get("FLS_BACKUP_ROLLBACK_RECOVERY") else LIMIT_SCRIPT if os.environ.get("FLS_BACKUP_LIMIT_RECOVERY") else CANCEL_SCRIPT if os.environ.get("FLS_BACKUP_CANCEL") else IDENTITY_SCRIPT if os.environ.get("FLS_RESTORE_IDENTITY") else LOCAL_RESTORE_STATE_SCRIPT if os.environ.get("FLS_BACKUP_RESTORE_LOCAL_STATE") else RESTORE_SCRIPT if os.environ.get("FLS_BACKUP_RESTORE") else SCRIPT
            result = subprocess.run(["node", "-e", script], env=env, timeout=180)
            if result.returncode:
                raise SystemExit(result.returncode)
        finally:
            release.set()
            archive_release.set()
            tarfile.TarFile.add = add_archive
            server.shutdown()
            scheduler.scheduler.shutdown(wait=False)


ARCHIVE_INDEX_SCRIPT = r'''
const assert=require('node:assert/strict');
const {chromium}=require(process.env.FLS_PLAYWRIGHT_CORE);
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.FLS_CHROMIUM,args:['--no-sandbox']});
 try{
  for(const [viewport,indexCopy] of [[{width:390,height:844},'primary'],[{width:1280,height:800},'backup']]){
   const context=await browser.newContext({viewport}),page=await context.newPage();
   page.on('dialog',dialog=>dialog.accept());const errors=[];page.on('pageerror',error=>errors.push(error.message));
   const base=process.env.FLS_UI_BASE,headers={'X-Token':process.env.FLS_UI_TOKEN};
   const fixture=async data=>{const response=await page.request.post(base+'/api/fixture/backup',{headers,data});assert.equal(response.status(),200);return response.json();};
   const state=async()=>(await page.request.get(base+'/api/fixture/backup',{headers})).json();
   const form=()=>page.locator('form[action="/backup/import"]');
   const history=()=>page.locator('#backupRestoreHistoryItems');
   const recovery=()=>page.locator('#backupRestoreRecovery');
   const archive={name:'restore.tar.gz',mimeType:'application/gzip',buffer:Buffer.from(process.env.FLS_RESTORE_ARCHIVE,'base64')};
   await page.goto(base+'/login');await page.locator('[name="token"]').fill(process.env.FLS_UI_TOKEN);
   await page.locator('button[type="submit"]').click();await page.waitForURL(url=>url.pathname!=='/login');
   const archivedRequestId='0'.repeat(32),archiveHash=require('node:crypto').createHash('sha256').update(archive.buffer).digest('hex');
   await fixture({reset_restore_history:true,seed_archive_candidate:true,archive_sha256:archiveHash});
   await page.goto(base+'/backup');const before=(await state()).restores;
   await form().locator('[name="file"]').setInputFiles(archive);
   const restoreRequestId=await form().locator('[name="restore_request_id"]').inputValue();
   const submitted=page.waitForResponse(response=>new URL(response.url()).pathname==='/backup/import'&&response.request().method()==='POST');
   await form().locator('button[type="submit"]').click();assert.equal((await submitted).status(),200);
   assert.equal((await state()).restores,before+1);
   await page.goto(base+'/backup');
   await page.getByRole('button',{name:'查询恢复历史',exact:true}).click();
   await history().getByText(new RegExp(archivedRequestId)).waitFor();
   await history().getByText(new RegExp(restoreRequestId)).waitFor();
   await fixture({corrupt_archive_index:indexCopy});
   await page.reload();await page.getByRole('button',{name:'查询恢复历史',exact:true}).click();
   await history().getByText(/恢复归档索引副本已自动合并并修复/).waitFor();
   const archivedRow=history().locator(':scope > div').filter({hasText:archivedRequestId});
   await archivedRow.getByRole('button',{name:'核对此请求',exact:true}).click();
   await recovery().getByText(/已归档，不会再次覆盖目录/).waitFor();
   const maintenanceEventId=await page.evaluate(()=>window.flsRestoreHistoryVerification.maintenance.event_id);
   await fixture({ack_directory_fsync_failure:true});
   const acknowledge=recovery().getByRole('button',{name:'确认已读并清除维护提示',exact:true});
   await acknowledge.waitFor();await acknowledge.click();
   await recovery().getByText(/维护提示未能清除.*HTTP 503/).waitFor();
   await recovery().getByText(/维护提示已确认，但目录同步尚未确认/).waitFor();
   let fakeAck=true;
   await page.route('**/api/backup/restore-maintenance/*/ack',async route=>{
    if(!fakeAck)return route.continue();fakeAck=false;
    await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({ok:true,cleared:false,maintenance:{event_id:maintenanceEventId,required:false,notice:'尚未清除'}})});
   });
   await recovery().getByRole('button',{name:'确认已读并清除维护提示',exact:true}).click();
   await recovery().getByText(/维护提示未能清除.*维护清除确认响应无效/).waitFor();
   assert.equal((await (await page.request.get(base+'/api/backup/restore-history?limit=100&offset=0',{headers})).json()).maintenance.event_id,maintenanceEventId);
   await recovery().getByRole('button',{name:'确认已读并清除维护提示',exact:true}).click();
   await recovery().getByText(/维护提示已确认清除/).waitFor();
   const cleared=await (await page.request.get(base+'/api/backup/restore-history?limit=100&offset=0',{headers})).json();
   assert.equal(cleared.maintenance,null);assert.equal(cleared.maintenance_error,'');
   assert.equal((await state()).restores,before+1);
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
   await page.evaluate(()=>sessionStorage.setItem('fls-backup-restore','{broken local state'));
   await fixture({corrupt_archive_index:indexCopy,fail_index_repair:true});
   await page.reload();
   await page.getByRole('button',{name:'核对恢复历史',exact:true}).click();
   await history().getByText(/冗余副本修复尚未完成/).waitFor();
   await assert.rejects(page.getByRole('button',{name:'确认清理已核对状态',exact:true}).waitFor({timeout:250}));
   await recovery().getByText(/归档索引冗余修复尚未完成/).waitFor();
   assert.equal((await state()).restores,before+1);
   if(process.env.FLS_BULK_SCREENSHOTS) await page.screenshot({path:process.env.FLS_BULK_SCREENSHOTS+'/backup-archive-index-'+viewport.width+'.png',fullPage:true});
   assert.deepEqual(errors,[]);console.log('backup archive index recovery passed '+viewport.width+' '+indexCopy);
   await context.close();
  }
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exit(1)});
'''


ATOMIC_SCRIPT = r'''
const assert=require('node:assert/strict');
const {chromium}=require(process.env.FLS_PLAYWRIGHT_CORE);
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.FLS_CHROMIUM,args:['--no-sandbox']});
 try{
  for(const viewport of [{width:390,height:844},{width:1280,height:800}]){
   const context=await browser.newContext({viewport}),page=await context.newPage();
   const errors=[];page.on('pageerror',e=>errors.push(e.message));page.on('dialog',d=>d.accept());
   const base=process.env.FLS_UI_BASE,headers={'X-Token':process.env.FLS_UI_TOKEN};
   const fixture=async data=>assert.equal((await page.request.post(base+'/api/fixture/backup',{headers,data})).status(),200);
   const state=async()=>(await page.request.get(base+'/api/fixture/backup',{headers})).json();
   const list=async()=>(await page.request.get(base+'/api/backup/list',{headers})).json();
   await fixture({});await page.goto(base+'/login');await page.locator('[name="token"]').fill(process.env.FLS_UI_TOKEN);
   await page.locator('button[type="submit"]').click();await page.waitForURL(u=>u.pathname!='/login');await page.goto(base+'/backup');
   const form=page.locator('#backupCreateForm'),progress=page.locator('#backupJobText');
   await form.locator('[name="items"][value="data"]').uncheck();
   const waitJob=async(id,status)=>{await page.waitForFunction(async({id,status})=>{
    const r=await fetch('/api/backup/job/'+id);return r.ok&&(await r.json()).status===status;
   },{id,status});await page.evaluate(id=>flsPollBackupJob(id),id);};
   const start=async fault=>{await fixture(fault);const id=await form.locator('[name="request_id"]').inputValue();
    await form.getByRole('button',{name:'创建备份',exact:true}).click();return id;};
   for(const cancel of [false,true]){
    const count=(await list()).items.length,id=await start({atomic_block:true});
    await page.waitForFunction(async()=>{const r=await fetch('/api/fixture/backup');return (await r.json()).archive_paused;});
    const staged=await state();assert.equal(staged.partials.length,1);assert.equal((await list()).items.length,count);
    assert.equal((await page.request.get(base+'/backup/download/'+staged.partials[0],{headers})).status(),404);
    const job=await page.request.get(base+'/api/backup/job/'+id,{headers});assert.equal((await job.json()).running,true);
    if(cancel){await page.evaluate(id=>flsPollBackupJob(id),id);await progress.getByRole('button',{name:'取消备份',exact:true}).click();}
    await fixture({});await waitJob(id,cancel?'已取消':'已完成');
    await progress.getByText(cancel?'已取消':'已完成',{exact:true}).waitFor();
    assert.equal((await list()).items.length,count+Number(!cancel));assert.equal((await state()).partials.length,0);
    assert.equal((await state()).active,0);
   }
   for(const mode of ['before','after','unknown']){
    const count=(await list()).items.length,id=await start({atomic_publish:mode});
    const status={before:'失败',after:'已完成',unknown:'结果未知'}[mode];await waitJob(id,status);
    await progress.getByText(status,{exact:true}).waitFor();
    const details=await progress.innerText();
    if(!details.includes('fixture archive publication fault'))throw new Error('publication diagnostic missing for '+mode+': '+details);
    assert.equal((await list()).items.length,count+Number(mode!=='before'));
    assert.equal((await state()).partials.length,0);
    if(mode==='unknown'){
     const before=(await state()).workers.length;
     assert.equal(await progress.getByRole('button',{name:'重新创建备份',exact:true}).count(),0);
     assert.equal(await form.getByRole('button',{name:'创建备份',exact:true}).isDisabled(),true);
     await progress.getByRole('button',{name:'查询已发布归档',exact:true}).click();
     const filename=(await list()).items[0].name;
     const download=await page.request.get(base+'/backup/download/'+filename,{headers});
     assert.equal(download.status(),200);assert.equal((await download.body()).subarray(0,2).toString('hex'),'1f8b');
     await progress.getByRole('button',{name:'确认后重新创建备份',exact:true}).waitFor();
     assert.equal((await state()).workers.length,before);
     await fixture({atomic_block:true});
     await progress.getByRole('button',{name:'确认后重新创建备份',exact:true}).click();
     const retryId=await form.locator('[name="request_id"]').inputValue();
     await page.waitForFunction(async()=>{const r=await fetch('/api/fixture/backup');return (await r.json()).archive_paused;});
     assert.equal((await state()).workers.length,before+1);
     await page.evaluate(id=>flsPollBackupJob(id),retryId);
     await progress.getByRole('button',{name:'取消备份',exact:true}).click();
     await waitJob(retryId,'已取消');
     await page.reload();await page.evaluate(id=>flsPollBackupJob(id),id);
     await progress.getByText('结果未知',{exact:true}).waitFor();
     assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
    await page.screenshot({path:'/tmp/fls-backup-atomic-'+viewport.width+'.png',fullPage:true});
    }
   }
   await fixture({orphan_stage:true});
   const orphanId=(await state()).orphan_job_id;
   const orphanStatus=await page.request.get(base+'/api/backup/job/'+orphanId,{headers});
   assert.equal(orphanStatus.status(),200);assert.equal((await orphanStatus.json()).status,'结果未知');
   assert.equal((await orphanStatus.json()).staged_cleanup_pending,false);
   assert.equal((await state()).partials.length,0);
   await page.evaluate(()=>sessionStorage.removeItem('fls-backup-request'));
   await page.goto(base+'/backup?job_id='+orphanId);
   await progress.getByText('结果未知',{exact:true}).waitFor();
   assert.equal(await form.getByRole('button',{name:'创建备份',exact:true}).isDisabled(),true);
   assert.equal(await form.locator('[name="items"][value="data"]').isChecked(),false);
   assert.equal(await form.locator('[name="items"][value="scripts"]').isChecked(),true);
   const orphanWorkers=(await state()).workers.length;
   await progress.getByRole('button',{name:'查询已发布归档',exact:true}).click();
   await progress.getByRole('button',{name:'确认后重新创建备份',exact:true}).waitFor();
   assert.equal((await state()).workers.length,orphanWorkers);
   await fixture({atomic_block:true});
   await progress.getByRole('button',{name:'确认后重新创建备份',exact:true}).click();
   const recoveryRetryId=await form.locator('[name="request_id"]').inputValue();
   await page.waitForFunction(async()=>{const r=await fetch('/api/fixture/backup');return (await r.json()).archive_paused;});
   assert.equal((await state()).workers.length,orphanWorkers+1);
   await page.evaluate(id=>flsPollBackupJob(id),recoveryRetryId);
   await progress.getByRole('button',{name:'取消备份',exact:true}).click();
   await fixture({});
   await waitJob(recoveryRetryId,'已取消');
   assert.equal((await state()).partials.length,0);
   await fixture({});assert.deepEqual(errors,[]);await context.close();console.log('backup atomic publication passed '+viewport.width);
  }
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
'''


CANCEL_SCRIPT = r'''
const assert=require('node:assert/strict');
const {chromium}=require(process.env.FLS_PLAYWRIGHT_CORE);
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.FLS_CHROMIUM,args:['--no-sandbox']});
 try {
  const base=process.env.FLS_UI_BASE,headers={'X-Token':process.env.FLS_UI_TOKEN};
  for(const viewport of [{width:390,height:844},{width:1280,height:800}]){
   const context=await browser.newContext({viewport});const page=await context.newPage();
   page.on('dialog',d=>d.accept());const errors=[];page.on('pageerror',e=>errors.push(e.message));
   const state=async()=>(await page.request.get(base+'/api/fixture/backup',{headers})).json();
   const fixture=async data=>assert.equal((await page.request.post(base+'/api/fixture/backup',{headers,data})).status(),200);
   const login=async()=>{await page.locator('input[name="token"]').fill(process.env.FLS_UI_TOKEN);await page.locator('button[type="submit"]').click();await page.waitForURL(u=>u.pathname!='/login');};
   const query=()=>page.getByRole('button',{name:'查询原备份请求',exact:true});
   const cancel=()=>page.getByRole('button',{name:'取消备份',exact:true});
   const create=()=>page.locator('#backupCreateForm button[type="button"]');
   const clickCancel=async button=>{const response=page.waitForResponse(r=>new URL(r.url()).pathname.startsWith('/api/backup/cancel/'));await button.click();return response;};
   const settled=async()=>{await page.waitForFunction(()=>!sessionStorage.getItem('fls-backup-request'));};
   await page.goto(base+'/login');await login();
   await fixture({block:true});await page.goto(base+'/backup');
   const created=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/backup/create');await create().click();
   const id=(await (await created).json()).job_id;await cancel().waitFor();
   // Direct progress links have no sessionStorage request; their recovery must still work.
   await page.evaluate(()=>sessionStorage.removeItem('fls-backup-request'));
   await page.goto(base+'/backup?job_id='+id);await cancel().waitFor();
   await fixture({block:true,cancel:'before'});
   assert.equal((await clickCancel(cancel())).status(),500);
   const retry=()=>page.getByRole('button',{name:'重试取消原备份',exact:true});await retry().waitFor();
   assert.equal((await state()).jobs.find(j=>j.id===id).cancel_requested,undefined);
   await fixture({block:true,cancel:'after'});
   const accepted=await clickCancel(retry());assert.equal(accepted.status(),503);assert.equal((await accepted.json()).accepted,true);
   await query().waitFor();
   if(process.env.FLS_BULK_SCREENSHOTS) await page.screenshot({path:process.env.FLS_BULK_SCREENSHOTS+'/backup-cancel-'+viewport.width+'.png',fullPage:true});
   await query().click();await page.getByText('正在取消',{exact:true}).waitFor();assert.equal(await cancel().count(),0);
   assert.equal((await page.request.post(base+'/api/backup/cancel/'+id,{headers})).status(),200);
   await fixture({});await page.waitForFunction(()=>document.querySelector('#backupJobText')?.textContent.includes('已取消'));
   assert.equal((await page.request.post(base+'/api/backup/cancel/'+id,{headers})).status(),200);
   // The next real job loses its cancel response; login and refresh query the same job.
   await fixture({block:true});await create().click();await cancel().waitFor();
   const original=(await state()).workers.length;
   await page.route('**/api/backup/cancel/*',async route=>{await route.fetch();await route.abort();},{times:1});
   await cancel().click();await query().waitFor();
   await page.goto(base+'/logout');await login();await page.goto(base+'/backup');
   await page.getByText('正在取消',{exact:true}).waitFor();assert.equal(await cancel().count(),0);
   await fixture({});await settled();assert.equal((await state()).workers.length,original);
   assert.match(await page.locator('#backupJobText').innerText(),/已取消/);
   // GET export leaves jobs unchanged; selection still reaches the POST create form.
   await page.goto(base+'/backup/export');assert.match(page.url(),/\/backup#backup-create$/);
   assert.equal((await state()).workers.length,original);
   assert.equal(await create().isEnabled(),true);
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
   assert.deepEqual(errors,[]);
   // Plain HTML requests use the same cancellation operation and retry the same job.
   const plain=await browser.newContext({viewport,javaScriptEnabled:false});const html=await plain.newPage();
   await html.goto(base+'/login');await html.locator('input[name="token"]').fill(process.env.FLS_UI_TOKEN);
   await html.locator('button[type="submit"]').click();await html.waitForURL(u=>u.pathname!='/login');
   await fixture({block:true});await html.goto(base+'/backup/export');
   await html.getByRole('button',{name:'提交备份',exact:true}).click();
   await html.getByRole('link',{name:'查看原备份进度',exact:true}).click();
   await fixture({block:true,cancel:'before'});await html.getByRole('button',{name:'取消备份',exact:true}).click();
   await html.getByText('取消请求未保存',{exact:false}).waitFor();
   await fixture({block:true});await html.getByRole('button',{name:'重试取消原备份',exact:true}).click();
   await fixture({});await html.getByRole('link',{name:'查询原备份进度',exact:true}).click();
   await html.getByText('状态：已取消',{exact:true}).waitFor();
   assert.equal(await html.getByRole('button',{name:'取消备份',exact:true}).count(),0);
   await plain.close();await context.close();console.log('backup cancel API/form recovery passed '+viewport.width);
  }
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
'''


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
   await page.goto(base+'/login');await login();
   const workersBeforeRetention=(await state()).workers.length;
   await fixture({retention:true});await page.goto(base+'/backup?job_id='+'a'.repeat(32));
   await page.waitForFunction(()=>document.getElementById('backupJobText')?.textContent.includes('结果未知'));
   const retained=await state();assert.equal(retained.jobs.find(row=>row.id==='a'.repeat(32)).status,'结果未知');
   assert.equal(retained.workers.length,workersBeforeRetention);
   const pendingAfterRestart=await page.evaluate(()=>JSON.parse(sessionStorage.getItem('fls-backup-request')));
   assert.equal(pendingAfterRestart.job_id,'a'.repeat(32));assert.equal(pendingAfterRestart.result_unknown,true);
   await page.evaluate(()=>sessionStorage.removeItem('fls-backup-request'));
   await page.goto(base+'/backup');
   const workersBeforeCorrupt=(await state()).workers.length;
   for(const [index,raw] of ['{broken','[]','', '{"request_id":"bad","items":["scripts"]}'].entries()){
    await page.evaluate(value=>sessionStorage.setItem('fls-backup-request',value),raw);
    await page.reload();
    const recoverCorrupt=()=>page.getByRole('button',{name:'刷新备份列表并清理无效请求',exact:true});
    await recoverCorrupt().waitFor();assert.equal(await create().isEnabled(),false);
    if(index===0){
     await page.route('**/api/backup/list',route=>route.fulfill({status:503,contentType:'text/plain',body:'fixture list failure'}),{times:1});
     await recoverCorrupt().click();
     assert.equal(await page.evaluate(()=>sessionStorage.getItem('fls-backup-request')),raw);
     assert.equal(await create().isEnabled(),false);
     await page.route('**/api/backup/list',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({ok:true,items:{}})}),{times:1});
     await recoverCorrupt().click();
     assert.equal(await page.evaluate(()=>sessionStorage.getItem('fls-backup-request')),raw);
     assert.equal(await create().isEnabled(),false);
    }
    await recoverCorrupt().click();
    await page.waitForFunction(()=>sessionStorage.getItem('fls-backup-request')===null);
    assert.equal(await create().isEnabled(),true);
    assert.equal((await state()).workers.length,workersBeforeCorrupt);
   }
   const initial=(await state()).workers.length;
   const fields=await page.locator('#backupCreateForm').evaluate(f=>({request_id:f.elements.request_id.value,items:['scripts']}));
   assert.equal((await page.request.post(base+'/api/backup/create',{data:fields,headers:{'X-CSRF-Token':'bad'}})).status(),400);
   const csrf=await page.locator('meta[name="csrf-token"]').getAttribute('content');
   const formRequestId=viewport.width.toString(16).padStart(32,'0');
   const formData={request_id:formRequestId,items:['scripts']};
   const formCreate=()=>page.request.post(base+'/api/backup/create',{form:formData,headers:{'X-CSRF-Token':csrf}});
   const formFirst=await formCreate();assert.equal(formFirst.status(),200);
   const formJob=(await formFirst.json()).job_id;assert.equal(formJob,formRequestId);
   const formReplay=await formCreate();assert.equal(formReplay.status(),200);
   assert.equal((await formReplay.json()).job_id,formJob);
   await page.waitForFunction(async id=>{const response=await fetch('/api/backup/job/'+id);const job=await response.json();return job.ok&&!job.running&&job.status==='已完成';},formJob);
   assert.equal((await state()).workers.length,initial+1);
   const beforeUi=(await state()).workers.length;
   assert.equal((await page.request.post(base+'/api/backup/create',{data:{...fields,items:['scripts','bad']},headers:{'X-CSRF-Token':csrf}})).status(),400);
   assert.equal((await state()).workers.length,beforeUi);
   await page.locator('#backupCreateForm [value="data"]').uncheck();
   // Real creation completes, but its HTTP response never reaches the page.
   await page.route('**/api/backup/create',async route=>{await route.fetch();await route.abort();},{times:1});
   await create().click();await query().waitFor();assert.equal((await state()).workers.length,beforeUi+1);
   await page.reload();await query().waitFor();
   await page.locator('#backupCreateForm [value="data"]').check();
   assert.equal((await submit(query())).status(),200);await settled();
   assert.equal((await state()).workers.length,beforeUi+1);
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


LOCAL_RESTORE_STATE_SCRIPT = r'''
const assert=require('node:assert/strict');
const {chromium}=require(process.env.FLS_PLAYWRIGHT_CORE);
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.FLS_CHROMIUM,args:['--no-sandbox']});
 try{
  for(const viewport of [{width:390,height:844},{width:1280,height:800}]){
   const context=await browser.newContext({viewport}),page=await context.newPage();
   page.on('dialog',d=>d.accept());const errors=[];page.on('pageerror',e=>errors.push(e.message));
   const base=process.env.FLS_UI_BASE,headers={'X-Token':process.env.FLS_UI_TOKEN};
   const fixture=async data=>assert.equal((await page.request.post(base+'/api/fixture/backup',{headers,data})).status(),200);
   const state=async()=>(await page.request.get(base+'/api/fixture/backup',{headers})).json();
   const form=()=>page.locator('form[action="/backup/import"]');
   const recovery=()=>page.locator('#backupRestoreRecovery');
   const history=()=>page.locator('#backupRestoreHistoryItems');
   const archive={name:'restore.tar.gz',mimeType:'application/gzip',buffer:Buffer.from(process.env.FLS_RESTORE_ARCHIVE,'base64')};
   await page.goto(base+'/login');await page.locator('[name="token"]').fill(process.env.FLS_UI_TOKEN);
   await page.locator('button[type="submit"]').click();await page.waitForURL(u=>u.pathname!='/login');
   const archivedRequestId='0'.repeat(32);
   const archiveHash=require('node:crypto').createHash('sha256').update(archive.buffer).digest('hex');
   await fixture({reset_restore_history:true,seed_archive_candidate:true,archive_sha256:archiveHash});await page.goto(base+'/backup');const initial=(await state()).restores;
   await form().locator('[name="file"]').setInputFiles(archive);
   const requestId=await form().locator('[name="restore_request_id"]').inputValue();
   let response=page.waitForResponse(r=>new URL(r.url()).pathname==='/backup/import'&&r.request().method()==='POST');
   await form().locator('button[type="submit"]').click();assert.equal((await response).status(),200);
   assert.equal((await state()).restores,initial+1);
   await page.goto(base+'/backup');
   await page.getByRole('button',{name:'查询恢复历史',exact:true}).click();
   await history().getByText(/older-restore\.tar\.gz \| restored \| 已归档/).waitFor();
   await page.locator('#backupRestoreHistoryItems > div').filter({hasText:archivedRequestId})
    .getByRole('button',{name:'核对此请求',exact:true}).click();
   await recovery().getByText(/已归档，不会再次覆盖目录/).waitFor();
   assert.equal((await state()).restores,initial+1);

   await page.evaluate(()=>sessionStorage.setItem('fls-backup-restore','{broken'));
   await page.reload();await recovery().getByRole('button',{name:'核对恢复历史',exact:true}).waitFor();
   let restorePosts=0;page.on('request',request=>{if(request.method()==='POST'&&new URL(request.url()).pathname==='/backup/import')restorePosts++;});
   await form().locator('[name="file"]').setInputFiles(archive);
   await form().locator('button[type="submit"]').click();await recovery().getByText(/不会提交新的目录覆盖/).waitFor();
   assert.equal(restorePosts,0);assert.equal((await state()).restores,initial+1);
   await recovery().getByRole('button',{name:'核对恢复历史',exact:true}).click();
   await history().getByText(new RegExp(requestId)).waitFor();
   await page.locator('#backupRestoreHistoryItems > div').filter({hasText:requestId})
    .getByRole('button',{name:'核对此请求',exact:true}).click();
   await recovery().getByText(/损坏状态仍保留/).waitFor();
   await recovery().getByRole('button',{name:'核对恢复历史',exact:true}).click();
   await recovery().getByRole('button',{name:'确认清理已核对状态',exact:true}).waitFor();
   await recovery().getByRole('button',{name:'确认清理已核对状态',exact:true}).click();
   assert.equal(await page.evaluate(()=>sessionStorage.getItem('fls-backup-restore')),null);
   assert.equal(await page.evaluate(()=>sessionStorage.getItem('fls-backup-restore-corrupt')),null);
   assert.notEqual(await form().locator('[name="restore_request_id"]').inputValue(),requestId);

   await fixture({unknown:requestId});
   await page.evaluate(()=>sessionStorage.setItem('fls-backup-restore',JSON.stringify({restore_request_id:'invalid',restore_items:['data'],restore_deps:false})));
   await page.reload();await recovery().getByRole('button',{name:'核对恢复历史',exact:true}).waitFor();
   await recovery().getByRole('button',{name:'核对恢复历史',exact:true}).click();
   await history().getByText(/restore\.tar\.gz \| unknown/).waitFor();
   assert.equal(await recovery().getByRole('button',{name:'确认清理已核对状态',exact:true}).count(),0);
   await page.locator('#backupRestoreHistoryItems > div').filter({hasText:requestId})
    .getByRole('button',{name:'核对此请求',exact:true}).click();
   await recovery().getByRole('button',{name:'仅重新加载调度器',exact:true}).waitFor();
   await recovery().getByRole('button',{name:'核对恢复历史',exact:true}).click();
   await recovery().getByText(/含有尚未完成或结果未知/).waitFor();
   await form().locator('[name="file"]').setInputFiles(archive);
   await form().locator('button[type="submit"]').click();await recovery().getByText(/不会提交新的目录覆盖/).waitFor();
   assert.equal(restorePosts,0);assert.equal((await state()).restores,initial+1);
   assert.equal(await page.evaluate(()=>sessionStorage.getItem('fls-backup-restore-corrupt')),'{\"restore_request_id\":\"invalid\",\"restore_items\":[\"data\"],\"restore_deps\":false}');
   assert.deepEqual(errors,[]);console.log('backup restore local state recovery passed '+viewport.width);
   await context.close();
  }
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
'''


RESTORE_SCRIPT = r'''
const assert=require('node:assert/strict');
const {chromium}=require(process.env.FLS_PLAYWRIGHT_CORE);
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.FLS_CHROMIUM,args:['--no-sandbox']});
 try{
  for(const viewport of [{width:390,height:844},{width:1280,height:800}]){
   const context=await browser.newContext({viewport}),page=await context.newPage();
   page.on('dialog',d=>d.accept());const errors=[];page.on('pageerror',e=>errors.push(e.message));
   const base=process.env.FLS_UI_BASE,headers={'X-Token':process.env.FLS_UI_TOKEN};
   const fixture=async data=>assert.equal((await page.request.post(base+'/api/fixture/backup',{headers,data})).status(),200);
   const state=async()=>(await page.request.get(base+'/api/fixture/backup',{headers})).json();
   const upload=()=>page.locator('form[action="/backup/import"]');
   const archive={name:'restore.tar.gz',mimeType:'application/gzip',buffer:Buffer.from(process.env.FLS_RESTORE_ARCHIVE,'base64')};
   const submit=async(form,path)=>{
    const response=page.waitForResponse(r=>new URL(r.url()).pathname===path && r.request().method()==='POST');
    await form.locator('button[type="submit"]').click();const res=await response;
    await page.waitForFunction(()=>!document.querySelector('.fls-loading-overlay.active'));return res;
   };
   await page.goto(base+'/login');await page.locator('[name="token"]').fill(process.env.FLS_UI_TOKEN);
   await page.locator('button[type="submit"]').click();await page.waitForURL(u=>u.pathname!='/login');
   await page.goto(base+'/backup');const initial=(await state()).restores;
   await upload().locator('[name="file"]').setInputFiles(archive);
   await upload().evaluate(form=>{const input=document.createElement('input');input.type='hidden';input.name='restore_items';input.value='bad';form.append(input);});
   assert.equal((await submit(upload(),'/backup/import')).status(),400);
   assert.equal((await state()).restores,initial);
   await page.goto(base+'/backup');await page.getByRole('button',{name:'核对后开始新的恢复',exact:true}).click();
   await upload().locator('[name="file"]').setInputFiles(archive);
   await fixture({reload:true});assert.equal((await submit(upload(),'/backup/import')).status(),503);
   assert.equal((await state()).restores,initial+1);assert.equal((await state()).script,"print('restored')");
   assert.equal(await page.locator('form[action="/backup/import"]').count(),0);
   let recovery=page.locator('form[action^="/backup/restore/"][action$="/reload"]');await recovery.waitFor();
   const restoreReloadAction=await recovery.getAttribute('action');
   const restoreRequestId=restoreReloadAction.match(/\/backup\/restore\/([a-f0-9]{32})\/reload$/)[1];
   if(process.env.FLS_BULK_SCREENSHOTS) await page.screenshot({path:process.env.FLS_BULK_SCREENSHOTS+'/backup-restore-failure-'+viewport.width+'.png',fullPage:true});
   const fields=await recovery.evaluate(f=>Object.fromEntries(new FormData(f)));
   assert.equal((await page.request.post(base+restoreReloadAction,{form:{...fields,csrf_token:'bad'}})).status(),400);
   assert.equal((await submit(recovery,restoreReloadAction)).status(),503);
   // Persisted scheduler failure retains a recovery action after a fresh navigation.
   await page.goto(base+'/backup');
   await page.locator('#backupRestoreRecovery').getByRole('button',{name:'查询原恢复请求',exact:true}).click();
   await page.locator('#backupRestoreRecovery').getByRole('button',{name:'仅重新加载调度器',exact:true}).waitFor();
   await page.getByRole('button',{name:'仅重新加载已保存任务',exact:true}).waitFor();
   await fixture({edit:true,reload:true});
   const api=await page.request.post(base+'/api/scheduler/reload',{headers});assert.equal(api.status(),500);
   await fixture({});
   recovery=page.locator('form[action^="/scheduler/reload"]');
   await page.route(url=>url.pathname==='/scheduler/reload',async route=>{await route.fetch();await route.abort();},{times:1});
   const lost=page.waitForEvent('requestfailed',{predicate:r=>new URL(r.url()).pathname==='/scheduler/reload'});
   await recovery.locator('button').click();
   await lost;
   await page.locator('.fls-toast').filter({hasText:'请求失败'}).waitFor();
   await page.waitForFunction(()=>!document.querySelector('form[action^="/scheduler/reload"][data-submitting="true"]'));
   await submit(recovery,'/scheduler/reload');await page.waitForURL(u=>u.pathname==='/backup');
   assert.equal((await state()).restores,initial+1);assert.equal((await state()).script,"print('later edit')");
   assert.equal((await state()).tasks[0].name,'Later edit');assert.ok((await state()).scheduled.includes('task_restored'));
   assert.equal((await page.request.post(base+'/api/scheduler/reload',{headers})).status(),200);
   assert.equal((await state()).restores,initial+1);
   assert.equal((await page.request.get(base+'/api/backup/restore/'+restoreRequestId)).status(),503);
   await page.locator('#backupRestoreRecovery').getByRole('button',{name:'查询原恢复请求',exact:true}).click();
   const scopedRecovery=page.locator('#backupRestoreRecovery').getByRole('button',{name:'仅重新加载调度器',exact:true});
   await scopedRecovery.waitFor();await scopedRecovery.click();
   const restoreStatus=await page.request.get(base+'/api/backup/restore/'+restoreRequestId);
   assert.equal(restoreStatus.status(),200);assert.equal((await restoreStatus.json()).resolved,true);
   assert.equal((await state()).script,"print('later edit')");assert.equal((await state()).tasks[0].name,'Later edit');
   // Actual multipart API uses the same partial-state contract and recovery endpoint.
   await fixture({reload:true});
   const response=await page.request.post(base+'/api/backup/import',{headers,multipart:{file:archive,restore_items:'scripts'}});
   assert.equal(response.status(),503);const result=await response.json();assert.equal(result.restored,true);
   assert.deepEqual(result.restored_items,['scripts']);assert.equal(result.reload_url,'/api/scheduler/reload');
   await fixture({});assert.equal((await page.request.post(base+result.reload_url,{headers})).status(),200);
   await page.goto(base+'/backup');
   await page.getByRole('button',{name:'核对后开始新的恢复',exact:true}).click();
   await upload().locator('[name="file"]').setInputFiles(archive);
   await upload().locator('[name="restore_deps"]').check();
   assert.equal((await submit(upload(),'/backup/import')).status(),503);
   await page.getByRole('link',{name:'查看并修复依赖',exact:true}).waitFor();
   assert.equal(await page.locator('form[action="/backup/import"]').count(),0);
   if(process.env.FLS_BULK_SCREENSHOTS) await page.screenshot({path:process.env.FLS_BULK_SCREENSHOTS+'/backup-restore-'+viewport.width+'.png',fullPage:true});
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);assert.deepEqual(errors,[]);
   console.log('backup restore recovery passed '+viewport.width);await context.close();
  }
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
'''


LIMIT_SCRIPT = r'''
const assert=require('node:assert/strict');
const {chromium}=require(process.env.FLS_PLAYWRIGHT_CORE);
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.FLS_CHROMIUM,args:['--no-sandbox']});
 try{
  for(const viewport of [{width:390,height:844},{width:1280,height:800}]){
   const context=await browser.newContext({viewport}),page=await context.newPage();
   page.on('dialog',d=>d.accept());const errors=[];page.on('pageerror',e=>errors.push(e.message));
   const base=process.env.FLS_UI_BASE,headers={'X-Token':process.env.FLS_UI_TOKEN};
   const fixture=async data=>assert.equal((await page.request.post(base+'/api/fixture/backup',{headers,data})).status(),200);
   const state=async()=>(await page.request.get(base+'/api/fixture/backup',{headers})).json();
   const archive={name:'too-large.tar.gz',mimeType:'application/gzip',buffer:Buffer.from(process.env.FLS_RESTORE_ARCHIVE,'base64')};
   await page.goto(base+'/login');await page.locator('[name="token"]').fill(process.env.FLS_UI_TOKEN);
   await page.locator('button[type="submit"]').click();await page.waitForURL(u=>u.pathname!='/login');
   await page.goto(base+'/backup');const initial=await state();
   await fixture({archive_limit:true});
   const form=page.locator('form[action="/backup/import"]');
   await form.locator('[name="file"]').setInputFiles(archive);
   const requestId=await form.locator('[name="restore_request_id"]').inputValue();
   const response=page.waitForResponse(r=>new URL(r.url()).pathname==='/backup/import'&&r.request().method()==='POST');
   await form.locator('button[type="submit"]').click();
   assert.equal((await response).status(),413);
   await page.getByText('备份导入失败',{exact:false}).waitFor();
   await page.getByRole('link',{name:'返回备份恢复',exact:true}).click();
   const recovery=page.locator('#backupRestoreRecovery');
   const query=recovery.getByRole('button',{name:'查询原恢复请求',exact:true});
   await query.waitFor();await query.click();
   await recovery.getByRole('button',{name:'选择原文件继续恢复',exact:true}).waitFor();
   await recovery.getByText('备份文件解压后超过允许大小',{exact:false}).waitFor();
   assert.equal(JSON.parse(await page.evaluate(()=>sessionStorage.getItem('fls-backup-restore'))).restore_request_id,requestId);
   const after=await state();assert.equal(after.restores,initial.restores);
   assert.equal((await page.request.get(base+'/api/backup/restore/'+requestId,{headers})).status(),500);
   await recovery.getByRole('button',{name:'核对后开始新的恢复',exact:true}).click();
   assert.equal(await page.evaluate(()=>sessionStorage.getItem('fls-backup-restore')),null);
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
   assert.deepEqual(errors,[]);console.log('backup limit recovery passed '+viewport.width);
   await context.close();
  }
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
'''


ROLLBACK_SCRIPT = r'''
const assert=require('node:assert/strict');
const {chromium}=require(process.env.FLS_PLAYWRIGHT_CORE);
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.FLS_CHROMIUM,args:['--no-sandbox']});
 try{
  const width=Number(process.env.FLS_BACKUP_ROLLBACK_VIEWPORT||390);
  for(const viewport of [{width,height:width===390?844:800}]){
   const context=await browser.newContext({viewport}),page=await context.newPage();
   page.on('dialog',d=>d.accept());const errors=[];page.on('pageerror',e=>errors.push(e.message));
   const base=process.env.FLS_UI_BASE,headers={'X-Token':process.env.FLS_UI_TOKEN};
   const fixture=async data=>assert.equal((await page.request.post(base+'/api/fixture/backup',{headers,data})).status(),200);
   const state=async()=>(await page.request.get(base+'/api/fixture/backup',{headers})).json();
   const archive={name:'rollback.tar.gz',mimeType:'application/gzip',buffer:Buffer.from(process.env.FLS_RESTORE_ARCHIVE,'base64')};
   await page.goto(base+'/login');await page.locator('[name="token"]').fill(process.env.FLS_UI_TOKEN);
   await page.locator('button[type="submit"]').click();await page.waitForURL(u=>u.pathname!='/login');
   await page.goto(base+'/backup');const initial=await state();
   await fixture({rollback:true});
   const form=page.locator('form[action="/backup/import"]');
   await form.locator('[name="file"]').setInputFiles(archive);
   const requestId=await form.locator('[name="restore_request_id"]').inputValue();
   const response=page.waitForResponse(r=>new URL(r.url()).pathname==='/backup/import'&&r.request().method()==='POST');
   await form.locator('button[type="submit"]').click();
   const uploadResponse=await response;
   assert.equal(uploadResponse.status(),503,await uploadResponse.text());
   await page.getByText('现有目录状态可能不完整',{exact:false}).waitFor();
   await page.getByRole('link',{name:'返回备份恢复',exact:true}).click();
   const recovery=page.locator('#backupRestoreRecovery');
   const query=recovery.getByRole('button',{name:'查询原恢复请求',exact:true});
   await query.waitFor();await query.click();
   await recovery.getByText('fixture script rollback failure',{exact:false}).waitFor();
   assert.equal(await recovery.getByRole('button',{name:'选择原文件继续恢复',exact:true}).count(),0);
   assert.match(await recovery.innerText(),/不要重新导入/);
   assert.equal(JSON.parse(await page.evaluate(()=>sessionStorage.getItem('fls-backup-restore'))).restore_request_id,requestId);
   const after=await state();assert.equal(after.restores,initial.restores+1);assert.equal(after.script,"");
   const status=await page.request.get(base+'/api/backup/restore/'+requestId,{headers});
   assert.equal(status.status(),503);assert.equal((await status.json()).state,'unknown');
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
   assert.deepEqual(errors,[]);console.log('backup rollback recovery passed '+viewport.width);
   await fixture({});await context.close();
  }
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
'''


IDENTITY_SCRIPT = r'''
const assert=require('node:assert/strict');
const {chromium}=require(process.env.FLS_PLAYWRIGHT_CORE);
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.FLS_CHROMIUM,args:['--no-sandbox']});
 try{
  for(const viewport of [{width:390,height:844},{width:1280,height:800}]){
   const context=await browser.newContext({viewport}),page=await context.newPage();
   page.on('dialog',d=>d.accept());const errors=[];page.on('pageerror',e=>errors.push(e.message));
   const base=process.env.FLS_UI_BASE,headers={'X-Token':process.env.FLS_UI_TOKEN};
   const fixture=async data=>assert.equal((await page.request.post(base+'/api/fixture/backup',{headers,data})).status(),200);
   const state=async()=>(await page.request.get(base+'/api/fixture/backup',{headers})).json();
   const form=()=>page.locator('form[action="/backup/import"]');
   const box=()=>page.locator('#backupRestoreRecovery');
   const query=()=>box().getByRole('button',{name:'查询原恢复请求',exact:true});
   const archive={name:'restore.tar.gz',mimeType:'application/gzip',buffer:Buffer.from(process.env.FLS_RESTORE_ARCHIVE,'base64')};
   const login=async()=>{
    await page.locator('[name="token"]').fill(process.env.FLS_UI_TOKEN);
    await page.locator('button[type="submit"]').click();await page.waitForURL(u=>u.pathname!='/login');
   };
   const submit=async()=>{
    const response=page.waitForResponse(r=>new URL(r.url()).pathname==='/backup/import'&&r.request().method()==='POST');
    await form().locator('button[type="submit"]').click();return response;
   };
   const lookup=async()=>{
    const response=page.waitForResponse(r=>new URL(r.url()).pathname.startsWith('/api/backup/restore/'));
    await query().click();return response;
   };
   await page.goto(base+'/login');await login();await page.goto(base+'/backup');
   let initial=(await state()).restores;
   await form().locator('[name="file"]').setInputFiles(archive);
   const original=await form().locator('[name="restore_request_id"]').inputValue();
   // A successful directory replacement loses its HTTP response.
   await page.route('**/backup/import',async route=>{await route.fetch();await route.abort();},{times:1});
   await form().locator('button[type="submit"]').click();await page.locator('.fls-toast').filter({hasText:'请求失败'}).waitFor();
   assert.equal((await state()).restores,initial+1);await fixture({edit:true});
   await page.reload();await query().waitFor();
   await form().locator('[name="file"]').setInputFiles(archive);
   await form().locator('button[type="submit"]').click();
   assert.equal((await state()).restores,initial+1);
   assert.equal((await lookup()).status(),200);
   await page.waitForFunction(()=>!sessionStorage.getItem('fls-backup-restore'));
   assert.equal((await state()).script,"print('later edit')");assert.equal((await state()).tasks[0].name,'Later edit');
   // API replay uses the identical archive, returning the first result.
   const replayFields=new FormData();replayFields.append('file',new Blob([archive.buffer]),archive.name);
   replayFields.append('restore_items','data');replayFields.append('restore_items','scripts');replayFields.append('restore_request_id',original);
   const replay=await page.request.post(base+'/api/backup/import',{headers,multipart:replayFields});
   assert.equal(replay.status(),200);assert.equal((await state()).restores,initial+1);
   // A gateway failure before delivery and fresh login keep the same request ID.
   await page.route('**/backup/import',r=>r.fulfill({status:502,contentType:'text/plain',body:'fixture gateway'}),{times:1});
   await form().locator('[name="file"]').setInputFiles(archive);await form().locator('button[type="submit"]').click();
   await page.locator('.fls-toast').filter({hasText:'fixture gateway'}).waitFor();
   await page.goto(base+'/logout');await login();await page.goto(base+'/backup');
   assert.equal((await lookup()).status(),404);
   await box().getByRole('button',{name:'选择原文件继续恢复',exact:true}).click();
   await form().locator('[name="file"]').setInputFiles(archive);assert.equal((await submit()).status(),200);
   await page.goto(base+'/backup');assert.equal((await lookup()).status(),200);
   await page.waitForFunction(()=>!sessionStorage.getItem('fls-backup-restore'));
   assert.equal((await state()).restores,initial+2);
   // Received receipt failure resumes the original archive without an extra restore.
   await fixture({restore_receipt:true});await form().locator('[name="file"]').setInputFiles(archive);
   assert.equal((await submit()).status(),500);await fixture({});await page.goto(base+'/backup');
   assert.equal((await lookup()).status(),200);
   await box().getByRole('button',{name:'选择原文件继续恢复',exact:true}).click();
   await form().locator('[name="file"]').setInputFiles(archive);assert.equal((await submit()).status(),200);
   await page.goto(base+'/backup');assert.equal((await lookup()).status(),200);
   await page.waitForFunction(()=>!sessionStorage.getItem('fls-backup-restore'));
   assert.equal((await state()).restores,initial+3);
   // An interrupted committing request is unknown, and offers reload rather than reupload.
   await fixture({unknown:original});
   await page.evaluate(id=>sessionStorage.setItem('fls-backup-restore',JSON.stringify({restore_request_id:id,restore_items:['data','scripts'],restore_deps:false})),original);
   await page.reload();assert.equal((await lookup()).status(),503);
   await box().getByRole('button',{name:'仅重新加载调度器',exact:true}).click();
   await box().getByText('已重新加载当前任务',{exact:false}).waitFor();
   assert.equal((await state()).restores,initial+3);
   await page.evaluate(()=>{document.documentElement.style.scrollBehavior='auto';document.activeElement?.blur();window.scrollTo(0,0);});
   if(process.env.FLS_BULK_SCREENSHOTS) await page.screenshot({path:process.env.FLS_BULK_SCREENSHOTS+'/backup-restore-identity-'+viewport.width+'.png',fullPage:true});
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);assert.deepEqual(errors,[]);
   console.log('restore identity recovery passed '+viewport.width);await context.close();
  }
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
'''


if __name__ == "__main__":
    main()
