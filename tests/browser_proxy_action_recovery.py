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
        from fls_manager import notify_actions, notify_test_actions, notify_retry_actions, notify
        from fls_manager.notify import send_by_ids
        from fls_manager.config import load_config
        from fls_manager.config import save_config
        from fls_manager.routes.proxy import actions
        from fls_manager.routes.notify import actions as notify_routes

        save_config({"notify_items": [], "notify_default_ids": []})
        app = create_app()
        if any(os.environ.get(key) for key in ("FLS_NOTIFY_TESTS", "FLS_NOTIFY_SAVES", "FLS_NOTIFY_RETRIES")):
            import fls_manager.app as app_module
            app_module.dispatch_frontend_open_event = lambda: None
        fault = {}
        save, receipt = models.save_proxies, proxy_actions.write_json
        notify_receipt = notify_actions.write_json
        test_receipt, delivery_write = notify_test_actions.write_json, notify.write_json
        retry_receipt = notify_retry_actions.write_json
        deliveries = []
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        class Webhook(BaseHTTPRequestHandler):
            def do_POST(self):
                deliveries.append(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
                self.send_response(503 if fault.get("test_network") else 200)
                self.end_headers()
                self.wfile.write(b"accepted")
            def log_message(self, *args):
                pass
        webhook = ThreadingHTTPServer(("127.0.0.1", 0), Webhook)
        threading.Thread(target=webhook.serve_forever, daemon=True).start()

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

        def persist_notify(values):
            if fault.get("notify_save"):
                raise OSError("fixture notification rejected")
            save_config(values)
            if fault.get("notify_after"):
                raise OSError("fixture notification confirmation rejected")

        def write_notify(path, row):
            if fault.get("notify_receipt") and row["state"] != "prepared":
                raise OSError("fixture notification receipt rejected")
            return notify_receipt(path, row)

        def write_test(path, row):
            if fault.get("test_request"):
                raise OSError("fixture test request rejected")
            return test_receipt(path, row)

        def write_delivery(path, rows):
            status = rows[0]["status"] if rows else ""
            if fault.get("test_history") and status == "running":
                raise OSError("fixture test history rejected")
            if (fault.get("test_result") or fault.get("test_result_after")) and status == "succeeded":
                if fault.get("test_result_after"):
                    delivery_write(path, rows)
                raise OSError("fixture delivery confirmation rejected")
            if fault.get("retry_history") and status == "running":
                raise OSError("fixture retry claim rejected")
            if fault.get("retry_result") and status == "succeeded":
                raise OSError("fixture retry result rejected")
            return delivery_write(path, rows)

        def write_retry(path, row):
            if fault.get("retry_receipt") and row["state"] != "prepared":
                raise OSError("fixture retry receipt rejected")
            return retry_receipt(path, row)

        models.save_proxies = actions.save_proxies = persist
        proxy_actions.write_json = write
        notify_routes.save_config = persist_notify
        notify_actions.write_json = write_notify
        notify_test_actions.write_json = write_test
        notify.write_json = write_delivery
        notify_retry_actions.write_json = write_retry

        @app.route("/api/fixture/proxy", methods=["GET", "POST"])
        def fixture():
            if request.method == "GET":
                cfg = load_config()
                return jsonify(proxies=models.load_proxies(), tasks=models.load_tasks(), history=models.load_task_history(),
                               notify=cfg.get("notify_items", []), defaults=cfg.get("notify_default_ids", []), deliveries=deliveries,
                               delivery_history=notify.load_notify_history())
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
            if data.get("notify_reset"):
                deliveries.clear()
                save_config({"notify_items": [
                    {"id": value, "name": "Webhook " + value, "enabled": True, "channel": "webhook", "log_mode": "full",
                     "config": {"WEBHOOK_URL": f"http://127.0.0.1:{webhook.server_port}/{value}"}} for value in ("n", "other")],
                     "notify_default_ids": ["n", "other"]})
                models.save_tasks([{"id": "run", "name": "Notify reference", "command": "echo notification", "notify": {"mode": "custom" if data.get("notify_reference") else "none", "ids": ["n"] if data.get("notify_reference") else []}}])
            if data.get("notify_changed"):
                cfg = load_config()
                cfg["notify_items"][0]["name"] = "Changed notification"
                save_config(cfg)
            if data.get("notify_remove"):
                cfg = load_config()
                cfg["notify_items"] = [row for row in cfg["notify_items"] if row["id"] != data["notify_remove"]]
                save_config(cfg)
            if data.get("retry_seed"):
                item = load_config()["notify_items"][0]
                delivery_id = notify.create_notify_delivery(item, "Retry browser title", "Retry browser content")
                notify.finish_notify_delivery(delivery_id, False, "Initial browser rejection")
            if data.get("retry_changed"):
                rows = notify.read_notify_history_rows()
                rows[0]["attempt"] += 1
                rows[0]["message"] = "Changed externally"
                delivery_write(notify.NOTIFY_HISTORY_FILE, rows)
            if data.get("notify_send"):
                results = send_by_ids("Saved defaults", "Actual payload", ["__default__"])
                return jsonify(ok=all(row["ok"] for row in results), ids=[row["id"] for row in results])
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
            webhook.shutdown()
            webhook.server_close()
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
   if(process.env.FLS_NOTIFY_RETRIES){
    await fixture({notify_reset:true,retry_seed:true});
    await page.goto(base+'/login');await login();await page.goto(base+'/notify/history');
    const rform=()=>page.locator('form[action^="/notify/retry/"]').first();
    const rnotice=async text=>{await page.locator('[data-notify-result]').waitFor();assert.match(await page.locator('[data-notify-result]').innerText(),text);};
    const count=async()=>(await state()).deliveries.length;
    const stale=await rform().evaluate(f=>Object.fromEntries(new FormData(f)));
    const oldUrl=await rform().getAttribute('action');
    assert.equal((await page.request.post(base+oldUrl,{form:{...stale,csrf_token:'bad'}})).status(),400);
    await fixture({retry_changed:true});
    assert.equal((await submit(rform())).status(),409);await rnotice(/已变化/);assert.equal(await count(),0);
    await page.goto(base+'/notify/history');
    const originalFields=await rform().evaluate(f=>Object.fromEntries(new FormData(f)));
    await fixture({test_network:true});
    await page.route('**/notify/retry/*',async route=>{await route.fetch();await route.abort();},{times:1});
    await rform().locator('button').click();await page.locator('[data-collection-save-pending]').waitFor();
    assert.equal(await count(),1);await page.reload();
    await page.getByRole('button',{name:'查询本次保存结果',exact:true}).click();await rnotice(/重试发送失败/);
    await page.waitForFunction(()=>!document.querySelector('[data-collection-save-pending]'));
    assert.equal(await count(),1);assert.equal((await state()).delivery_history[0].attempt,3);
    assert.equal((await submit(rform())).status(),502);await rnotice(/重试发送失败/);assert.equal(await count(),2);
    const replay=await page.request.post(base+oldUrl,{form:originalFields});
    assert.equal(replay.status(),502);assert.equal(await count(),2);assert.equal((await state()).delivery_history[0].attempt,4);
    await fixture({});assert.equal((await submit(rform())).status(),200);await rnotice(/发送成功/);assert.equal(await count(),3);
    // Claim publication failure keeps the old selection and only increments once on retry.
    await fixture({retry_seed:true});await page.goto(base+'/notify/history');await fixture({retry_history:true});
    assert.equal((await submit(rform())).status(),500);await rnotice(/无法确认/);assert.equal(await count(),3);
    await fixture({});assert.equal((await submit(rform())).status(),200);assert.equal(await count(),4);
    // A claimed-but-unsent interruption is unknown and resumes through an explicit new attempt.
    await fixture({retry_seed:true});await page.goto(base+'/notify/history');await fixture({retry_receipt:true});
    assert.equal((await submit(rform())).status(),503);await rnotice(/结果未知/);assert.equal(await count(),4);
    await fixture({});assert.equal((await submit(rform())).status(),200);assert.equal(await count(),5);
    // Result publication failure after real delivery offers an executable recovery.
    await fixture({retry_seed:true});await page.goto(base+'/notify/history');await fixture({retry_result:true});
    assert.equal((await submit(rform())).status(),503);await rnotice(/结果未知/);assert.equal(await count(),6);
    await page.evaluate(()=>{document.documentElement.style.scrollBehavior='auto';window.scrollTo(0,0);});await page.waitForTimeout(100);
    if(process.env.FLS_BULK_SCREENSHOTS) await page.screenshot({path:process.env.FLS_BULK_SCREENSHOTS+'/notify-retry-'+viewport.width+'.png',fullPage:true});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
    await fixture({});assert.equal((await submit(rform())).status(),200);assert.equal(await count(),7);
    // Non-JSON 502 and fresh login query the original attempt after navigation.
    await fixture({retry_seed:true});await page.goto(base+'/notify/history');
    await page.route('**/notify/retry/*',r=>r.fulfill({status:502,contentType:'text/plain',body:'fixture gateway'}),{times:1});
    await rform().locator('button').click();await page.locator('[data-collection-save-pending]').waitFor();
    await page.goto(base+'/logout');await login();await page.getByRole('button',{name:'查询本次保存结果',exact:true}).click();
    await rnotice(/发送成功/);assert.equal(await count(),8);
    await fixture({retry_seed:true});await page.goto(base+'/notify/history');
    const fields=await rform().evaluate(f=>Object.fromEntries(new FormData(f)));
    const apiUrl=(await rform().getAttribute('action')).replace('/notify/retry/','/api/notify/retry/');
    await fixture({test_network:true});
    assert.equal((await page.request.post(base+apiUrl,{headers,data:fields})).status(),502);
    assert.equal((await page.request.post(base+apiUrl,{headers,data:fields})).status(),502);assert.equal(await count(),9);
    // Deleted channels can explicitly move the original title/body to a valid replacement.
    await fixture({retry_seed:true});await page.goto(base+'/notify/history');await fixture({notify_remove:'n'});
    assert.equal((await submit(rform())).status(),409);await rnotice(/删除或停用/);assert.equal(await count(),9);
    await page.getByLabel('选择重试通知渠道',{exact:true}).selectOption('other');
    const replaceForm=page.locator('form:has(select[name="item_id"])');
    await fixture({});assert.equal((await submit(replaceForm)).status(),200);await rnotice(/发送成功/);assert.equal(await count(),10);
    assert.ok((await state()).deliveries.at(-1).includes('Retry browser content'));
    assert.equal((await state()).delivery_history[0].item_id,'other');
    assert.deepEqual(errors,[]);console.log('notification retry recovery passed '+viewport.width);
    await context.close();continue;
   }
   if(process.env.FLS_NOTIFY_SAVES){
    await fixture({notify_reset:true});
    await page.goto(base+'/login');await login();await page.goto(base+'/notify/new');
    const nnotice=async text=>{await page.locator('[data-notify-result]').first().waitFor();assert.match(await page.locator('[data-notify-result]').first().innerText(),text);};
    const editForm=()=>page.locator('form:has(select[name="channel"])');
    const saveNotice=async (kind='save')=>{
     const f=editForm(),path=new URL(await f.getAttribute('action'),base).pathname;
     const pending=page.waitForResponse(r=>new URL(r.url()).pathname===path&&r.request().method()==='POST');
     await f.locator('button[name="action"][value="'+kind+'"]').click();
     const res=await pending;await page.waitForFunction(()=>!document.querySelector('.fls-loading-overlay.active'));return res;
    };
    const fill=async name=>{
     await page.getByLabel('通知名称',{exact:true}).fill(name);
     await page.getByLabel('通知渠道',{exact:true}).selectOption('webhook');
     await page.getByLabel('请求 URL',{exact:true}).fill((await state()).notify[0].config.WEBHOOK_URL);
     await page.getByLabel('请求体',{exact:true}).fill('Saved draft\n$content');
     await page.getByLabel('请求头',{exact:true}).fill('Authorization: browser-private');
    };
    await fill('Browser saved notification');
    await page.getByLabel('通知渠道',{exact:true}).selectOption('bark');
    await page.getByLabel('Bark 地址或设备码',{exact:true}).fill('kept-bark-draft');
    await page.getByLabel('通知渠道',{exact:true}).selectOption('webhook');
    assert.equal(await page.getByLabel('请求体',{exact:true}).inputValue(),'Saved draft\n$content');
    await page.getByLabel('通知模式',{exact:true}).evaluate(s=>{s.add(new Option('Invalid','bad',true,true));});
    assert.equal((await saveNotice()).status(),400);await nnotice(/通知模式无效/);
    assert.equal(await page.getByLabel('请求体',{exact:true}).inputValue(),'Saved draft\n$content');
    assert.equal((await state()).notify.length,2);
    await page.getByLabel('通知模式',{exact:true}).selectOption('trimmed');
    await fixture({notify_save:true});
    assert.equal((await saveNotice()).status(),500);await nnotice(/保存失败/);
    const requestId=await editForm().locator('[name="request_id"]').inputValue();
    await page.getByLabel('通知名称',{exact:true}).fill('Corrected saved notification');
    assert.equal(await editForm().locator('[name="request_id"]').inputValue(),requestId);
    await fixture({notify_after:true});
    assert.equal((await saveNotice()).status(),503);await nnotice(/已保存/);
    assert.equal((await state()).notify.length,3);
    await fixture({});await page.getByRole('link',{name:'查看已保存通知',exact:true}).click();
    assert.equal(await page.getByLabel('通知名称',{exact:true}).inputValue(),'Corrected saved notification');
    assert.equal(await page.getByLabel('请求头',{exact:true}).inputValue(),'Authorization: browser-private');
    // A stale edit retains the draft and requires an explicit merge against current settings.
    await page.goto(base+'/notify/edit/n');await page.getByLabel('通知名称',{exact:true}).fill('Merged notification');
    await fixture({notify_changed:true});assert.equal((await saveNotice()).status(),409);await nnotice(/已变化/);
    assert.match(await page.locator('.content').innerText(),/Changed notification/);
    assert.equal(await page.getByLabel('通知名称',{exact:true}).inputValue(),'Merged notification');
    await saveNotice();await page.waitForURL(u=>u.pathname==='/notify');
    assert.equal((await state()).notify[0].name,'Merged notification');
    // Lost save+test response must not create another channel or repeat the webhook.
    await page.goto(base+'/notify/new');await fill('Lost save and test');
    await page.route('**/notify/new',async route=>{await route.fetch();await route.abort();},{times:1});
    await editForm().locator('button[value="test"]').click();await page.locator('[data-collection-save-pending]').waitFor();
    assert.equal((await state()).notify.length,4);assert.equal((await state()).deliveries.length,1);
    await page.reload();await page.getByRole('button',{name:'查询本次保存结果',exact:true}).click();
    await nnotice(/通知已保存/);await page.waitForFunction(()=>!document.querySelector('[data-collection-save-pending]'));
    assert.equal((await state()).notify.length,4);assert.equal((await state()).deliveries.length,1);
    // Persistence confirmation failure resumes only the test, with the exact saved version.
    await page.goto(base+'/notify/new');await fill('Partial saved test');await fixture({notify_after:true});
    assert.equal((await saveNotice('test')).status(),503);await nnotice(/已保存/);
    assert.equal((await state()).notify.length,5);assert.equal((await state()).deliveries.length,1);
    await fixture({});await submit(page.locator('form[action^="/notify/test/"]'));
    await nnotice(/已发送/);assert.equal((await state()).deliveries.length,2);
    // A failed webhook offers the original history row, not another save.
    await page.goto(base+'/notify/edit/n');await fixture({test_network:true});
    assert.equal((await saveNotice('test')).status(),502);await nnotice(/通知已保存/);
    assert.equal(await page.locator('form:has(select[name="channel"])').count(),0);
    await fixture({});await submit(page.locator('form[action^="/notify/retry/"]'));
    assert.equal((await state()).deliveries.length,4);assert.equal((await state()).notify.length,5);
    // A gateway error and fresh login preserve both the draft and original create identifier.
    await page.goto(base+'/notify/new');await fill('Login recovery');
    await page.route('**/notify/new',r=>r.fulfill({status:502,contentType:'text/plain',body:'gateway'}),{times:1});
    await editForm().locator('button[value="save"]').click();await page.locator('[data-collection-save-pending]').waitFor();
    await page.goto(base+'/logout');await login();await page.getByRole('button',{name:'查询本次保存结果',exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector('[data-collection-save-pending]'));
    assert.equal((await state()).notify.length,6);
    // Interrupted save can be confirmed, and a deleted channel can explicitly save as new.
    await page.goto(base+'/notify/edit/n');await fixture({notify_save:true,notify_receipt:true});
    assert.equal((await saveNotice()).status(),500);await nnotice(/确认后重试/);
    await fixture({});await saveNotice();await page.waitForURL(u=>u.pathname==='/notify');
    await page.goto(base+'/notify/edit/n');await page.getByLabel('通知名称',{exact:true}).fill('Deleted draft');
    await fixture({notify_remove:'n'});assert.equal((await saveNotice()).status(),404);await nnotice(/不存在/);
    assert.equal(await editForm().getAttribute('action'),'/notify/new');
    await saveNotice();await page.waitForURL(u=>u.pathname==='/notify');assert.equal((await state()).notify.length,6);
    // Leave a real conflict recovery page for visual verification.
    await fixture({notify_reset:true});await page.goto(base+'/notify/edit/n');await fill('Preserved final draft');
    await fixture({notify_changed:true});assert.equal((await saveNotice()).status(),409);await nnotice(/已变化/);
    await page.evaluate(()=>{document.documentElement.style.scrollBehavior='auto';window.scrollTo(0,0);});await page.waitForTimeout(150);
    if(process.env.FLS_BULK_SCREENSHOTS) await page.screenshot({path:process.env.FLS_BULK_SCREENSHOTS+'/notify-save-'+viewport.width+'.png',fullPage:true});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
    assert.deepEqual(errors,[]);console.log('notification save recovery passed '+viewport.width);
    await context.close();continue;
   }
   if(process.env.FLS_NOTIFY_TESTS){
    await fixture({notify_reset:true});
    await page.goto(base+'/login');await login();await page.goto(base+'/notify');
    const tform=()=>page.locator('form[action="/notify/test/n"]:visible').first();
    const tnotice=async text=>{await page.locator('[data-notify-result]').waitFor();assert.match(await page.locator('[data-notify-result]').innerText(),text);};
    const count=async()=>(await state()).deliveries.length;
    const fields=await tform().evaluate(f=>Object.fromEntries(new FormData(f)));
    await page.request.get(base+'/notify/test/n');assert.equal(await count(),0);
    assert.equal((await page.request.post(base+'/notify/test/n',{form:{...fields,csrf_token:'bad'}})).status(),400);
    await fixture({notify_changed:true});
    assert.equal((await submit(tform())).status(),409);await tnotice(/已变化/);assert.equal(await count(),0);
    await page.goto(base+'/notify');await fixture({test_history:true});
    assert.equal((await submit(tform())).status(),500);await tnotice(/无法确认/);assert.equal(await count(),0);
    await fixture({});assert.equal((await submit(tform())).status(),409);await tnotice(/确认后继续/);
    assert.equal((await submit(page.locator('form[action="/notify/test/n"]:has(input[name="confirm_retry"][value="1"])'))).status(),200);await tnotice(/已发送/);assert.equal(await count(),1);
    await page.getByRole('link',{name:'查看投递记录'}).click();await page.waitForFunction(()=>location.pathname==='/notify/history');
    const sentRow=page.locator('tr').filter({hasText:'FLS 通知测试'}).first();await sentRow.getByRole('status').filter({hasText:'成功'}).waitFor();
    // A lost completed response survives refresh and does not repeat the webhook.
    await page.goto(base+'/notify');
    await page.route('**/notify/test/n',async route=>{await route.fetch();await route.abort();},{times:1});
    await tform().locator('button').click();await page.locator('[data-collection-save-pending]').waitFor();
    assert.equal(await count(),2);await page.reload();
    await page.getByRole('button',{name:'查询本次保存结果',exact:true}).click();
    await tnotice(/已发送/);await page.waitForFunction(()=>!document.querySelector('[data-collection-save-pending]'));
    assert.equal(await count(),2);
    // A failed delivery is retried explicitly through its persisted history row.
    await page.goto(base+'/notify');await fixture({test_network:true});
    const failedFields=await tform().evaluate(f=>Object.fromEntries(new FormData(f)));
    assert.equal((await submit(tform())).status(),502);await tnotice(/发送失败/);assert.equal(await count(),3);
    await fixture({});await submit(page.locator('form[action^="/notify/retry/"]'));
    await page.waitForURL(u=>u.pathname.startsWith('/notify/retry/'));assert.equal(await count(),4);
    assert.equal((await page.request.post(base+'/notify/test/n',{form:failedFields})).status(),200);assert.equal(await count(),4);
    // A missing completion receipt is unknown and provides an executable retry.
    await page.goto(base+'/notify');await fixture({test_result:true});
    assert.equal((await submit(tform())).status(),503);await tnotice(/结果未知/);assert.equal(await count(),5);
    await page.evaluate(()=>{document.documentElement.style.scrollBehavior='auto';window.scrollTo(0,0);});await page.waitForTimeout(100);
    if(process.env.FLS_BULK_SCREENSHOTS) await page.screenshot({path:process.env.FLS_BULK_SCREENSHOTS+'/notify-test-'+viewport.width+'.png',fullPage:true});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
    await fixture({});await submit(page.locator('form[action^="/notify/retry/"]'));assert.equal(await count(),6);
    await page.goto(base+'/notify');await fixture({test_result_after:true});
    assert.equal((await submit(tform())).status(),503);await tnotice(/已发送/);assert.equal(await count(),7);
    assert.equal(await page.locator('form[action^="/notify/retry/"]').count(),0);
    // A gateway response and re-login preserve the original pending request.
    await fixture({});await page.goto(base+'/notify');
    await page.route('**/notify/test/n',r=>r.fulfill({status:502,contentType:'text/plain',body:'fixture gateway'}),{times:1});
    await tform().locator('button').click();await page.locator('[data-collection-save-pending]').waitFor();
    await page.goto(base+'/logout');await login();
    await page.getByRole('button',{name:'查询本次保存结果',exact:true}).click();await tnotice(/已发送/);
    assert.equal(await count(),8);
    await page.goto(base+'/notify');
    const apiFields=await tform().evaluate(f=>Object.fromEntries(new FormData(f)));
    assert.equal((await page.request.post(base+'/api/notify/test/n',{headers,data:apiFields})).status(),200);
    assert.equal((await page.request.post(base+'/api/notify/test/n',{headers,data:apiFields})).status(),200);
    assert.equal(await count(),9);
    assert.deepEqual(errors,[]);console.log('notification test recovery passed '+viewport.width);
    await context.close();continue;
   }
   if(process.env.FLS_NOTIFY_ACTIONS){
    await fixture({notify_reset:true});
    await page.goto(base+'/login');await login();await page.goto(base+'/notify');
    const nform=action=>page.locator('form[action="/notify/'+action+(action==='default'?'':'/n')+'"]:visible').first();
    const nnotice=async text=>{await page.locator('[data-notify-result]').waitFor();assert.match(await page.locator('[data-notify-result]').innerText(),text);};
    const defaults=async()=>[...(await state()).defaults].sort();
    await fixture({notify_save:true});
    assert.equal((await submit(nform('toggle'))).status(),500);await nnotice(/保存失败/);
    assert.deepEqual(await defaults(),['n','other']);
    await fixture({notify_after:true});
    assert.equal((await submit(nform('toggle'))).status(),503);await nnotice(/已保存/);
    assert.deepEqual(await defaults(),['other']);
    await fixture({});await page.goto(base+'/notify');await submit(nform('toggle'));
    await page.waitForURL(u=>u.pathname==='/notify');
    assert.equal((await state()).notify[0].enabled,true);
    // A stale default form cannot discard later changes and retains valid selections.
    await nform('default').locator('select').selectOption(['n','other']);
    await fixture({notify_changed:true});
    assert.equal((await submit(nform('default'))).status(),409);await nnotice(/已变化/);
    await page.getByLabel('重新核对默认通知').selectOption(['n','other']);
    await submit(nform('default'));await page.waitForURL(u=>u.pathname==='/notify');
    assert.deepEqual(await defaults(),['n','other']);
    const fields=await nform('default').evaluate(f=>Object.fromEntries(new FormData(f)));
    assert.equal((await page.request.post(base+'/notify/default',{form:{...fields,default_ids:'missing'}})).status(),400);
    assert.deepEqual(await defaults(),['n','other']);
    assert.equal((await page.request.post(base+'/notify/delete/n',{form:{csrf_token:'bad'}})).status(),400);
    await nform('default').locator('select').selectOption(['other']);
    await fixture({notify_save:true});
    assert.equal((await submit(nform('default'))).status(),500);await nnotice(/保存失败/);
    assert.deepEqual(await nform('default').locator('[name="default_ids"]').evaluateAll(v=>v.map(x=>x.value)),['other']);
    await fixture({});await submit(nform('default'));await page.waitForURL(u=>u.pathname==='/notify');
    const delivered=await page.request.post(base+'/api/fixture/proxy',{headers,data:{notify_send:true}});
    assert.deepEqual((await delivered.json()).ids,['other']);
    assert.ok((await state()).deliveries.some(body=>body.includes('Actual payload')));
    // Custom task references block both stop and delete, with an executable task form.
    await fixture({notify_reset:true,notify_reference:true});await page.goto(base+'/notify');
    assert.equal((await submit(nform('delete'))).status(),409);await nnotice(/任务单独引用/);
    await page.getByRole('link',{name:'设置任务：Notify reference',exact:true}).click();
    await page.waitForURL(u=>u.pathname==='/task/edit/run');
    await page.locator('[name="notify_mode"][value="none"]').check();
    await page.locator('button[type="submit"]').filter({hasText:'保存任务'}).click();
    await page.waitForURL(u=>u.pathname!='/task/edit/run');
    await page.goto(base+'/notify');
    // The delete response is lost after one atomic config write; old replay cannot delete a replacement.
    await page.route('**/notify/delete/n',async route=>{await route.fetch();await route.abort();},{times:1});
    await nform('delete').locator('button').click();
    await page.locator('[data-collection-save-pending]').waitFor();
    assert.deepEqual(await defaults(),['other']);
    await fixture({notify_reset:true});await page.reload();
    await page.getByRole('button',{name:'查询本次保存结果',exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector('[data-collection-save-pending]'));
    assert.equal((await state()).notify.length,2);
    // Non-JSON failure, refresh and a fresh login keep all selected defaults.
    await page.goto(base+'/notify');await nform('default').locator('select').selectOption(['n','other']);
    await page.route('**/notify/default',r=>r.fulfill({status:502,contentType:'text/plain',body:'fixture unavailable'}),{times:1});
    await nform('default').locator('button').click();
    await page.locator('[data-collection-save-pending]').waitFor();
    await page.goto(base+'/logout');await login();
    await page.getByRole('button',{name:'查询本次保存结果',exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector('[data-collection-save-pending]'));
    assert.deepEqual(await defaults(),['n','other']);
    await fixture({notify_save:true,notify_receipt:true});await page.goto(base+'/notify');
    assert.equal((await submit(nform('toggle'))).status(),500);await nnotice(/确认后重试/);
    await fixture({});await submit(nform('toggle'));await page.waitForURL(u=>u.pathname==='/notify');
    assert.equal((await state()).notify[0].enabled,false);
    await fixture({notify_save:true});
    assert.equal((await submit(nform('delete'))).status(),500);await nnotice(/保存失败/);
    await page.evaluate(()=>{document.documentElement.style.scrollBehavior='auto';window.scrollTo(0,0);});await page.waitForTimeout(200);
    if(process.env.FLS_BULK_SCREENSHOTS) await page.screenshot({path:process.env.FLS_BULK_SCREENSHOTS+'/notify-actions-'+viewport.width+'.png',fullPage:true});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
    assert.deepEqual(errors,[]);console.log('notification actions passed '+viewport.width);
    await context.close();continue;
   }
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
   if(process.env.FLS_PROXY_SAVE){
    await fixture({reset:true});await page.goto(base+'/proxy/new');
    const fill=async(name,port='8080')=>{
     await page.locator('#proxyForm [name="name"]').fill(name);
     await page.locator('#proxyForm [name="type"]').selectOption('http');
     await page.locator('#proxyForm [name="host"]').fill('127.0.0.1');
     await page.locator('#proxyForm [name="port"]').fill(port);
     await page.locator('#proxyForm [name="password"]').fill('browser-private');
    };
    const saveForm=()=>page.locator('#proxyForm');
    const saveAction=async status=>assert.equal((await submit(saveForm())).status(),status);
    const proxyNamed=async name=>(await state()).proxies.filter(p=>p.name===name);
    await fill('Invalid draft','bad');await saveAction(400);
    await notice(/端口/);assert.equal(await page.locator('[name="password"]').inputValue(),'browser-private');
    assert.equal((await state()).proxies.length,1);
    await page.locator('[name="port"]').fill('8080');
    await fixture({save:true});await saveAction(500);await notice(/保存失败/);
    assert.equal(await page.locator('[name="name"]').inputValue(),'Invalid draft');
    await fixture({after:true});await saveAction(503);await notice(/已保存/);
    assert.equal((await proxyNamed('Invalid draft')).length,1);
    await page.getByRole('link',{name:'查看已保存代理',exact:true}).click();
    await page.waitForURL(u=>u.pathname.startsWith('/proxy/edit/'));
    const target=new URL(page.url()).pathname.split('/').pop();
    await fixture({});
    // A concurrent edit changes the version, while the draft stays editable.
    await page.locator('[name="name"]').fill('Merged browser');
    const current=(await state()).proxies.find(p=>p.id===target);
    const version=await page.locator('[name="version"]').inputValue();
    const update=await page.request.post(base+'/api/proxy/action',{headers,data:{...current,action:'edit',proxy_id:target,version,request_id:require('node:crypto').randomBytes(16).toString('hex'),name:'Concurrent browser'}});
    assert.equal(update.status(),200);
    await saveAction(409);await notice(/已变化/);
    assert.equal(await page.locator('[name="name"]').inputValue(),'Merged browser');
    await saveAction(302);await page.waitForURL(u=>u.pathname==='/proxy');
    assert.equal((await proxyNamed('Merged browser')).length,1);

    // Publish a creation, drop its response and ensure a new form cannot duplicate it.
    await page.goto(base+'/proxy/new');await fill('Lost creation');
    await page.route('**/proxy/new',async route=>{await route.fetch();await route.abort();},{times:1});
    await saveForm().locator('button[type="submit"]').click();
    await page.locator('[data-collection-save-pending]').waitFor();
    await page.reload();await fill('Must not duplicate');
    await saveForm().locator('button[type="submit"]').click();
    assert.equal((await proxyNamed('Must not duplicate')).length,0);
    await page.getByRole('button',{name:'查询本次保存结果',exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector('[data-collection-save-pending]'));
    assert.equal((await proxyNamed('Lost creation')).length,1);

    // Interrupted creation requires explicit confirmation without changing its identity.
    await page.goto(base+'/proxy/new');await fill('Interrupted creation');
    await fixture({save:true,receipt:true});await saveAction(500);await notice(/确认后重试/);
    const requestId=await page.locator('[name="request_id"]').inputValue();
    await fixture({});await saveAction(302);await page.waitForURL(u=>u.pathname==='/proxy');
    assert.equal((await proxyNamed('Interrupted creation')).length,1);
    assert.equal(requestId.length,32);

    // A missing edit target can explicitly become a new proxy.
    const lost=(await proxyNamed('Lost creation'))[0];
    await page.goto(base+'/proxy/edit/'+lost.id);await page.locator('[name="name"]').fill('Saved as new');
    const deleteVersion=await page.locator('[name="version"]').inputValue();
    assert.equal((await page.request.post(base+'/api/proxy/action',{headers,data:{action:'delete',proxy_id:lost.id,version:deleteVersion,request_id:require('node:crypto').randomBytes(16).toString('hex')}})).status(),200);
    await saveAction(404);await notice(/不存在/);
    assert.equal(await saveForm().getAttribute('action'),'/proxy/new');
    await saveAction(302);await page.waitForURL(u=>u.pathname==='/proxy');
    assert.equal((await proxyNamed('Saved as new')).length,1);

    await page.goto(base+'/proxy/new');await fill('After login');
    await page.route('**/proxy/new',r=>r.fulfill({status:502,contentType:'text/plain',body:'fixture gateway'}),{times:1});
    await saveForm().locator('button[type="submit"]').click();
    await page.locator('[data-collection-save-pending]').waitFor();
    await page.goto(base+'/logout');await login();
    await page.getByRole('button',{name:'查询本次保存结果',exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector('[data-collection-save-pending]'));
    assert.equal((await proxyNamed('After login')).length,1);
    await page.goto(base+'/proxy/new');await fill('Screenshot draft','bad');await saveAction(400);await notice(/端口/);
    await page.evaluate(()=>{document.documentElement.style.scrollBehavior='auto';document.activeElement?.blur();window.scrollTo(0,0);});
    await page.waitForTimeout(250);
    if(process.env.FLS_BULK_SCREENSHOTS) await page.screenshot({path:process.env.FLS_BULK_SCREENSHOTS+'/proxy-save-'+viewport.width+'.png',fullPage:true});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
    assert.deepEqual(errors,[]);
    console.log('proxy save passed '+viewport.width);
   }
   await context.close();
  }
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
'''


if __name__ == "__main__":
    main()
