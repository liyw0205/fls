"""Real HTTP/Chromium recovery for idempotent online task-link sync."""
import os
import tempfile
import threading
import subprocess
import uuid
from pathlib import Path

from tools.browser_regression import TOKEN, find_chromium, find_playwright_core, seed_browser_fixture


def main():
    playwright, chromium = find_playwright_core(), find_chromium()
    if not playwright or not chromium:
        raise SystemExit("Set FLS_PLAYWRIGHT_CORE and FLS_CHROMIUM")
    with tempfile.TemporaryDirectory(prefix="fls-task-link-browser-") as temp:
        seed_browser_fixture(temp)
        os.environ.update(FLS_BASE_DIR=temp, FLS_TOKEN=TOKEN, FLS_SECRET_KEY="task-link-browser")
        from flask import g, jsonify, request
        from werkzeug.serving import make_server
        from fls_manager.app import create_app
        import fls_manager.app as app_module
        from fls_manager import scheduler
        from fls_manager.online_scripts import constants, source
        from fls_manager.online_scripts.constants import update_online_refresh_state
        from fls_manager.online_scripts.logs import online_refresh_log_file
        from fls_manager.routes.online_scripts import task_link
        from fls_manager.routes.online_scripts import refresh

        item = {
            "id": "browser-link", "name": "浏览器任务源", "type": "raw",
            "link": "https://fixture.invalid/browser-link.py", "link_name": "browser-link.py",
            "task_link": "https://fixture.invalid/browser-link.json",
        }
        source.save_online_script_cache([item])
        fault = {"fail": True, "fetches": 0, "refreshes": 0, "fail_history_publication": False}
        real_fetch = task_link.fetch_task_link_tasks
        real_refresh_start = refresh.start_refresh_thread

        def start_refresh(refresh_id, _proxy_id, log_file):
            fault["refreshes"] += 1
            source.save_online_script_cache([dict(item, name="Recovered refresh script")])
            Path(log_file).write_text("original retry completed\n", encoding="utf-8")
            update_online_refresh_state(
                running=False, status="已完成", error="", message="fixture retry completed",
                cache_published=True, published_cache_fingerprint=source.online_script_cache_fingerprint(),
                log_file=log_file, updated_at="zz-completed",
            )

        refresh.start_refresh_thread = start_refresh

        def fetch(*_args, **_kwargs):
            fault["fetches"] += 1
            if fault["fail"]:
                raise RuntimeError("fixture task source unavailable")
            return [{"name": "Browser task", "command": "echo browser", "cron": ""}]

        task_link.fetch_task_link_tasks = fetch
        app = create_app()
        app_module.dispatch_frontend_open_event = lambda: None

        @app.before_request
        def inject_refresh_history_failure():
            if request.path != "/api/online-scripts/refresh" or not fault["fail_history_publication"]:
                return None
            fault["fail_history_publication"] = False
            g.refresh_replace = constants.os.replace
            original_replace = constants.os.replace

            def fail_history_once(src_path, dst):
                if Path(dst) == constants.ONLINE_REFRESH_HISTORY_FILE:
                    raise OSError("browser history publication denied")
                return original_replace(src_path, dst)

            constants.os.replace = fail_history_once

        @app.teardown_request
        def restore_refresh_replace(_error):
            original_replace = getattr(g, "refresh_replace", None)
            if original_replace is not None:
                constants.os.replace = original_replace

        @app.route("/api/fixture/task-link", methods=["GET", "POST"])
        def fixture():
            if request.method == "POST":
                data = request.get_json() or {}
                fault["fail"] = bool(data.get("fail"))
                if data.get("reset"):
                    fault["fetches"] = 0
                    source.save_online_script_cache([item])
            cached = source.load_online_script_cache()
            return jsonify(fail=fault["fail"], fetches=fault["fetches"], refreshes=fault["refreshes"], cached=cached)

        @app.route("/api/fixture/online-refresh", methods=["POST"])
        def refresh_fixture():
            data = request.get_json() or {}
            if data.get("clear"):
                update_online_refresh_state(
                    id="",
                    request_id="",
                    proxy_id="",
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
            running = bool(data.get("running"))
            status = "运行中" if running else "部分完成"
            message = "正在后台同步在线脚本源，请稍候..." if running else "同步在线脚本源部分完成，请逐项重试"
            refresh_id = str(data.get("refresh_id") or "e" * 32)
            refresh_request_id = str(data.get("request_id") or "d" * 32)
            update_online_refresh_state(
                id=refresh_id,
                request_id=refresh_request_id,
                proxy_id="",
                running=running,
                status=status,
                message=message,
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
                log_file=str(online_refresh_log_file(refresh_id)),
            )
            online_refresh_log_file(refresh_id).write_text(
                "fixture refresh log\n" + ("refresh running\n" if running else "refresh failed: upstream timeout\n"),
                encoding="utf-8",
            )
            return jsonify(ok=True)

        @app.route("/api/fixture/refresh-retry", methods=["POST"])
        def refresh_retry_fixture():
            fault["refreshes"] = 0
            original = uuid.uuid4().hex
            refresh_id = uuid.uuid4().hex
            path = online_refresh_log_file(refresh_id)
            path.write_text("original refresh failed\n", encoding="utf-8")
            update_online_refresh_state(
                id=refresh_id, request_id=original, proxy_id="", running=False,
                status="失败", error="fixture retry needed", message="", task_link_failures=[],
                cache_published=False, expected_cache_fingerprint=source.online_script_cache_fingerprint(),
                published_cache_fingerprint="", source_fingerprint=source.online_script_source_fingerprint(),
                retry_of="", confirmation="", confirmation_fingerprint="",
                log_file=str(path), updated_at="zz-original",
            )
            return jsonify(ok=True, original=original)

        @app.route("/api/fixture/refresh-publication-failure", methods=["POST"])
        def refresh_publication_failure_fixture():
            fault["refreshes"] = 0
            fault["fail_history_publication"] = True
            source.save_online_script_cache([item])
            update_online_refresh_state(
                id="", request_id="", proxy_id="", running=False, status="", message="", error="",
                cache_published=False, task_link_failures=[], updated_at="", log_file="",
            )
            return jsonify(cache_fingerprint=source.online_script_cache_fingerprint())

        @app.route("/api/fixture/refresh-publication-state")
        def refresh_publication_state_fixture():
            return jsonify(
                refreshes=fault["refreshes"], state=constants.get_online_refresh_state(),
                cache_fingerprint=source.online_script_cache_fingerprint(),
            )

        @app.route("/api/fixture/refresh-cache-unavailable", methods=["POST"])
        def refresh_cache_unavailable_fixture():
            cache = constants.ONLINE_SCRIPT_CACHE_FILE
            if (request.get_json() or {}).get("restore"):
                cache.rmdir()
                source.save_online_script_cache([item])
            else:
                cache.unlink()
                cache.mkdir()
            return jsonify(ok=True, refreshes=fault["refreshes"])

        server = make_server("127.0.0.1", 0, app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            env = dict(os.environ, FLS_UI_BASE=f"http://127.0.0.1:{server.server_port}",
                       FLS_UI_TOKEN=TOKEN, FLS_PLAYWRIGHT_CORE=str(playwright), FLS_CHROMIUM=str(chromium))
            subprocess.run(["node", "-e", SCRIPT], env=env, check=True, timeout=180)
        finally:
            task_link.fetch_task_link_tasks = real_fetch
            refresh.start_refresh_thread = real_refresh_start
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
   await page.evaluate(()=>sessionStorage.setItem('fls-online-refresh-request',JSON.stringify({request_id:'d'.repeat(32),proxy_id:'',server_id:'e'.repeat(32)})));
   await page.goto(base+'/online-scripts');
   await page.locator('#onlineRefreshLogLink').waitFor({state:'visible'});
   const originalStatus=await page.request.get(base+'/api/online-scripts/refresh-status/'+'d'.repeat(32),{headers});
   assert.equal(originalStatus.status(),200);
   assert.equal((await originalStatus.json()).request_id,'d'.repeat(32));
   await page.locator('#onlineRefreshLogLink').click();
   await page.waitForSelector('#refreshLogText');
   assert.match(await page.locator('#refreshLogText').textContent(),/fixture refresh log/);
   await page.request.post(base+'/api/fixture/online-refresh',{headers,data:{running:true}});
   await page.goto(base+'/online-scripts/refresh-log/'+'d'.repeat(32));
   await page.waitForFunction(()=>document.body.textContent.includes('运行中'));
   await page.request.post(base+'/api/fixture/online-refresh',{headers,data:{running:false}});
   await page.waitForFunction(()=>document.body.textContent.includes('部分完成'),{timeout:6000});
   await page.request.post(base+'/api/fixture/online-refresh',{headers,data:{request_id:'f'.repeat(32),refresh_id:'g'.repeat(32),running:false}});
   await page.goto(base+'/online-scripts/refresh-log/'+'d'.repeat(32));
   assert.match(await page.locator('#refreshLogText').textContent(),/refresh running|refresh failed/);
   await page.goto(base+'/online-scripts');
   await page.locator('#onlineRefreshHistory a[href="/online-scripts/refresh-log/'+'d'.repeat(32)+'"]').waitFor();
   const historyRetry=page.locator('#onlineRefreshHistory form[data-refresh-retry-of="'+'d'.repeat(32)+'"]');
   await historyRetry.getByRole('button',{name:'明确重新同步',exact:true}).waitFor();
   assert.match(await historyRetry.locator('[name="retry_request_id"]').inputValue(),/^[a-f0-9]{32}$/);
   await page.evaluate(()=>sessionStorage.setItem('fls-online-refresh-request',JSON.stringify({request_id:'d'.repeat(32),proxy_id:'',server_id:'e'.repeat(32)})));
   await page.locator('#onlineRefreshBtn').click();
   await page.waitForTimeout(1000);
   assert.match(await page.locator('.content').textContent(),/同步在线脚本源部分完成/);
   await page.evaluate(async()=>{await updateOnlineRefreshStatus();});
   const dynamicForm=page.locator('#onlineRefreshTaskLinkFailures form.fls-online-task-link-form');
   assert.equal(await dynamicForm.count(),1);
   let dynamicRequestId=await dynamicForm.locator('[name="request_id"]').inputValue();
   assert.match(dynamicRequestId,/^[a-f0-9]{32}$/);
   const refreshStatusPattern='**/api/online-scripts/refresh-status*';
   await page.route(refreshStatusPattern,route=>route.fulfill({status:503,contentType:'text/html',body:'temporary status unavailable'}));
   await page.evaluate(()=>updateOnlineRefreshStatus());
   await page.waitForFunction(()=>document.body.textContent.includes('HTTP 503'));
   const preservedDynamicForm=page.locator('#onlineRefreshTaskLinkFailures form.fls-online-task-link-form');
   assert.equal(await preservedDynamicForm.count(),1);
   assert.equal(await preservedDynamicForm.locator('[name="request_id"]').inputValue(),dynamicRequestId);
   assert.equal(await page.locator('#onlineRefreshLogLink').isVisible(),true);
   const statusQuery=page.getByRole('button',{name:'重新查询同步状态',exact:true});
   await statusQuery.click();
   await page.waitForFunction(()=>!document.getElementById('onlineRefreshQuery').disabled);
   assert.equal(await preservedDynamicForm.locator('[name="request_id"]').inputValue(),dynamicRequestId);
   await page.unroute(refreshStatusPattern);
   await statusQuery.click();
   await statusQuery.waitFor({state:'hidden'});
   dynamicRequestId=await dynamicForm.locator('[name="request_id"]').inputValue();
   assert.equal(await page.locator('#onlineRefreshLogLink').isVisible(),true);
   await dynamicForm.locator('button[type="submit"]').click();
   const dynamicRetry=page.getByRole('button',{name:'继续原同步请求',exact:true});
   await dynamicRetry.waitFor();
   assert.equal(await dynamicRetry.locator('xpath=ancestor::form').locator('[name="request_id"]').inputValue(),dynamicRequestId);
   assert.match(await page.locator('.content').textContent(),/fixture refresh task source unavailable/);
   await fixture({fail:false});
   await dynamicRetry.click();
   await page.waitForURL('**/online-scripts?msg=*');
   await page.waitForFunction(()=>document.body.textContent.includes('外部任务源同步完成'));
   const dynamicState=await fixture({});
   assert.equal(dynamicState.fetches,2);
   const setup=await page.request.post(base+'/api/fixture/refresh-retry',{headers,data:{}});
   assert.equal(setup.status(),200);
   const original=(await setup.json()).original;
   await page.goto(base+'/online-scripts');
   const refreshRetryForm=page.locator('#onlineRefreshHistory form[data-refresh-retry-of="'+original+'"]');
   await refreshRetryForm.waitFor();
   const retryRequestId=await refreshRetryForm.locator('[name="retry_request_id"]').inputValue();
   const retryEndpoint=base+'/api/online-scripts/refresh-retry/'+original;
   await page.route(retryEndpoint,async route=>{
     const actual=await route.fetch();
     assert.equal(actual.status(),202);
     await route.abort('failed');
   });
   await refreshRetryForm.getByRole('button',{name:'明确重新同步',exact:true}).click();
   await page.locator('#onlineRefreshQuery').waitFor({state:'visible'});
   assert.equal(await page.evaluate(()=>JSON.parse(sessionStorage.getItem('fls-online-refresh-request')).request_id),retryRequestId);
   await page.unroute(retryEndpoint);
   await page.locator('#onlineRefreshQuery').click();
   await page.waitForFunction(()=>!sessionStorage.getItem('fls-online-refresh-request'));
   await page.goto(base+'/online-scripts');
   const [replayed]=await Promise.all([
     page.waitForResponse(response=>response.url()===retryEndpoint&&response.request().method()==='POST'),
     refreshRetryForm.getByRole('button',{name:'明确重新同步',exact:true}).click(),
   ]);
   assert.equal(replayed.status(),200);
   const receipt=await replayed.json();
   assert.equal(receipt.replayed,true);
   assert.equal(receipt.request_id,retryRequestId);
   assert.equal(receipt.status,'已完成');
   const htmlReplay=await page.request.post(base+'/online-scripts/refresh-retry/'+original,{headers,form:{retry_request_id:retryRequestId,proxy_id:''}});
   assert.equal(htmlReplay.status(),200);
   assert.match(await htmlReplay.text(),/已查询原同步重试/);
   await page.goto(base+receipt.log_url);
   await page.waitForSelector('#refreshLogText');
   assert.match(await page.locator('#refreshLogText').textContent(),/original retry completed/);
   const current=await fixture({});
   assert.equal(current.refreshes,1);

   const publicationSetup=await page.request.post(base+'/api/fixture/refresh-publication-failure',{headers});
   assert.equal(publicationSetup.status(),200);
   const cacheFingerprint=(await publicationSetup.json()).cache_fingerprint;
   await page.goto(base+'/online-scripts');
   const refreshForm=page.locator('.fls-online-refresh-form');
   const publicationRequestId=await refreshForm.locator('[name="request_id"]').inputValue();
   await refreshForm.locator('button[type="submit"]').click();
   await page.waitForFunction(()=>document.body.textContent.includes('browser history publication denied'));
   assert.equal(JSON.parse(await page.evaluate(()=>sessionStorage.getItem('fls-online-refresh-request'))).request_id,publicationRequestId);
   await page.locator('#onlineRefreshQuery').click();
   const receiptRecovery=page.locator('#onlineRefreshPersistence form.fls-online-refresh-recover-form');
   await receiptRecovery.waitFor();
   assert.match(await page.locator('#onlineRefreshPersistence').textContent(),/未启动抓取/);
   const recoverEndpoint=base+'/api/online-scripts/refresh-recover/'+publicationRequestId;
   assert.equal(await receiptRecovery.getAttribute('data-api-action'),'/api/online-scripts/refresh-recover/'+publicationRequestId);
   await page.route(recoverEndpoint,route=>route.fulfill({status:503,contentType:'text/html',body:'receipt repair temporarily unavailable'}));
   await receiptRecovery.getByRole('button',{name:'修复刷新回执',exact:true}).click();
   await page.waitForFunction(()=>document.querySelector('#onlineRefreshStatusText').textContent.includes('HTTP 503'));
   assert.equal(await receiptRecovery.getByRole('button',{name:'修复刷新回执',exact:true}).isEnabled(),true);
   assert.equal(JSON.parse(await page.evaluate(()=>sessionStorage.getItem('fls-online-refresh-request'))).request_id,publicationRequestId);
   await page.unroute(recoverEndpoint);
   let repairs=0;
   await page.route(recoverEndpoint,async route=>{
     repairs++;
     const actual=await route.fetch();
     assert.equal(actual.status(),200);
     await route.abort('failed');
   });
   await receiptRecovery.getByRole('button',{name:'修复刷新回执',exact:true}).click();
   await page.waitForFunction(()=>!document.querySelector('#onlineRefreshPersistence form').dataset.pending && document.querySelector('#onlineRefreshStatusText').textContent.includes('回执修复未确认'));
   assert.equal(await receiptRecovery.getByRole('button',{name:'修复刷新回执',exact:true}).isEnabled(),true);
   await page.unroute(recoverEndpoint);
   await page.locator('#onlineRefreshQuery').click();
   await receiptRecovery.waitFor({state:'detached'});
   assert.equal(repairs,1);
   assert.equal(new URL(page.url()).pathname,'/online-scripts');
   const publicationState=await page.request.get(base+'/api/fixture/refresh-publication-state',{headers});
   assert.equal(publicationState.status(),200);
   const recoveredState=(await publicationState.json()).state;
   assert.equal((await publicationState.json()).refreshes,0);
   assert.equal(recoveredState.request_id,publicationRequestId);
   assert.equal(recoveredState.running,false);
   assert.equal(recoveredState.status,'结果未知');
   assert.equal(recoveredState.persistence_error,undefined);
   assert.equal((await publicationState.json()).cache_fingerprint,cacheFingerprint);
   const receiptLog=await page.request.get(base+'/api/online-scripts/refresh-log/'+publicationRequestId,{headers});
   assert.equal(receiptLog.status(),200);
   assert.match((await receiptLog.json()).log,/未启动抓取/);

   await page.request.post(base+'/api/fixture/online-refresh',{headers,data:{clear:true}});
   await page.evaluate(()=>sessionStorage.removeItem('fls-online-refresh-request'));
   await page.goto(base+'/online-scripts');
   const primaryForm=page.locator('.fls-online-refresh-form');
   const primaryId=await primaryForm.locator('[name="request_id"]').inputValue();
   const primaryEndpoint=base+'/api/online-scripts/refresh';
   await page.route(primaryEndpoint,async route=>{
     const actual=await route.fetch();
     assert.equal(actual.status(),202);
     await route.abort('failed');
   });
   await primaryForm.locator('button[type="submit"]').click();
   await page.locator('#onlineRefreshQuery').waitFor({state:'visible'});
   assert.equal(JSON.parse(await page.evaluate(()=>sessionStorage.getItem('fls-online-refresh-request'))).request_id,primaryId);
   await page.unroute(primaryEndpoint);
   const unavailable=await page.request.post(base+'/api/fixture/refresh-cache-unavailable',{headers,data:{}});
   assert.equal(unavailable.status(),200);
   assert.equal((await unavailable.json()).refreshes,1);
   const [primaryReplay]=await Promise.all([
     page.waitForResponse(response=>response.url()===primaryEndpoint&&response.request().method()==='POST'),
     primaryForm.locator('button[type="submit"]').click(),
   ]);
   assert.equal(primaryReplay.status(),200);
   const primaryReceipt=await primaryReplay.json();
   assert.equal(primaryReceipt.replayed,true);
   assert.equal(primaryReceipt.request_id,primaryId);
   assert.equal(primaryReceipt.status,'已完成');
   await page.waitForFunction(()=>!sessionStorage.getItem('fls-online-refresh-request'));
   const primaryHtml=await page.request.post(base+'/online-scripts/refresh',{headers,form:{request_id:primaryId,proxy_id:''}});
   assert.equal(primaryHtml.status(),200);
   assert.match(await primaryHtml.text(),/已查询原同步请求/);
   assert.match(await primaryHtml.text(),new RegExp(primaryReceipt.log_url));
   await page.goto(base+primaryReceipt.log_url);
   await page.waitForSelector('#refreshLogText');
   assert.match(await page.locator('#refreshLogText').textContent(),/original retry completed/);
   const restoredCache=await page.request.post(base+'/api/fixture/refresh-cache-unavailable',{headers,data:{restore:true}});
   assert.equal(restoredCache.status(),200);
   assert.equal((await restoredCache.json()).refreshes,1);
   assert.deepEqual(errors,[]);
   console.log('online task-link recovery passed '+viewport.width);
   await context.close();
  }
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
'''


if __name__ == "__main__":
    main()
