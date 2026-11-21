"""Real HTTP/Chromium recovery for idempotent online task-link sync."""
import json
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
        fault = {"fail": True, "fetches": 0, "refreshes": 0, "fail_history_publication": False,
                 "fail_retry_start": False, "fail_retry_receipt": False}
        real_fetch = task_link.fetch_task_link_tasks
        real_persist = task_link.persist_task_link_request
        real_refresh_start = refresh.start_refresh_thread

        def start_refresh(refresh_id, _proxy_id, log_file):
            if fault["fail_retry_start"]:
                fault["fail_retry_start"] = False
                if fault["fail_retry_receipt"]:
                    fault["fail_retry_receipt"] = False
                    real_persist_refresh = constants.persist_online_refresh

                    def fail_startup_receipt(info):
                        if info.get("status") == "启动失败" and info.get("request_id") == constants.ONLINE_REFRESH_STATE.get("request_id"):
                            raise OSError("browser retry status disk full")
                        return real_persist_refresh(info)

                    constants.persist_online_refresh = fail_startup_receipt
                    g.refresh_persist = real_persist_refresh
                raise RuntimeError("browser retry thread unavailable")
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

        def persist(record):
            mode = fault.get("startup_failure")
            if mode and record["state"] == "running":
                fault["startup_failure"] = ""
                if mode == "after":
                    real_persist(record)
                raise OSError("browser startup receipt unavailable")
            return real_persist(record)

        task_link.persist_task_link_request = persist
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
            original_persist = getattr(g, "refresh_persist", None)
            if original_persist is not None:
                constants.persist_online_refresh = original_persist

        @app.route("/api/fixture/task-link", methods=["GET", "POST"])
        def fixture():
            if request.method == "POST":
                data = request.get_json() or {}
                fault["fail"] = bool(data.get("fail"))
                if "startup_failure" in data:
                    fault["startup_failure"] = data["startup_failure"]
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
                    task_link_failure_count=0,
                    task_link_failure_digest=constants._refresh_task_failure_digest([]),
                    updated_at="",
                    log_file="",
                )
                return jsonify(ok=True)
            running = bool(data.get("running"))
            status = "运行中" if running else "部分完成"
            message = "正在后台同步在线脚本源，请稍候..." if running else "同步在线脚本源部分完成，请逐项重试"
            refresh_id = str(data.get("refresh_id") or "e" * 32)
            refresh_request_id = str(data.get("request_id") or "d" * 32)
            failure_count = int(data.get("failure_count", 1))
            failures = []
            if failure_count > 1:
                cache_items = []
                for number in range(failure_count):
                    failed_item = dict(
                        item,
                        id=f"browser-link-{number:03d}",
                        name=f"浏览器任务源 {number:03d}",
                        link=f"https://fixture.invalid/browser-link-{number:03d}.py",
                        task_link=f"https://fixture.invalid/browser-link-{number:03d}.json",
                    )
                    cache_items.append(failed_item)
                    failures.append({
                        "script_id": failed_item["id"],
                        "name": failed_item["name"],
                        "task_link": failed_item["task_link"],
                        "proxy_id": "",
                        "error": "fixture refresh task source unavailable",
                    })
                source.save_online_script_cache(cache_items)
            else:
                failures = [{
                    "script_id": item["id"],
                    "name": item["name"],
                    "task_link": item["task_link"],
                    "proxy_id": "",
                    "error": "fixture refresh task source unavailable",
                }]
            update_online_refresh_state(
                id=refresh_id,
                request_id=refresh_request_id,
                proxy_id="",
                running=running,
                status=status,
                message=message,
                error="",
                cache_published=True,
                task_link_failures=failures,
                task_link_failure_count=len(failures),
                task_link_failure_digest=constants._refresh_task_failure_digest(failures),
                updated_at="now",
                log_file=str(online_refresh_log_file(refresh_id)),
            )
            online_refresh_log_file(refresh_id).write_text(
                "fixture refresh log\n" + ("refresh running\n" if running else "refresh failed: upstream timeout\n"),
                encoding="utf-8",
            )
            return jsonify(ok=True)

        @app.route("/api/fixture/task-link-cache", methods=["POST"])
        def task_link_cache_fixture():
            cache = source.ONLINE_SCRIPT_CACHE_FILE
            data = request.get_json() or {}
            if data.get("restore"):
                cache.rmdir()
                source.save_online_script_cache([item])
            elif data.get("add_task"):
                current = source.load_online_script_cache()
                current[0]["task_cron"] = [{"name": "Manual task", "command": "echo manual", "cron": ""}]
                source.save_online_script_cache(current)
            else:
                cache.unlink()
                cache.mkdir()
            return jsonify(fetches=fault["fetches"], cache_is_directory=cache.is_dir())

        @app.route("/api/fixture/refresh-recovery-damage", methods=["POST"])
        def refresh_recovery_damage_fixture():
            data = request.get_json() or {}
            request_id = str(data.get("request_id") or "")
            path = constants._online_refresh_task_recoveries_path(request_id)
            if data.get("copy") == "backup":
                path = constants._online_refresh_task_recoveries_backup_path(request_id)
            path.write_text("fixture recovery copy corruption", encoding="utf-8")
            return jsonify(ok=True, path=path.name)

        @app.route("/api/fixture/task-link-confirmation", methods=["POST"])
        def task_link_confirmation_fixture():
            prepared = (request.get_json() or {}).get("prepared") is True
            current = source.normalize_online_scripts(source.load_online_script_cache())[0]
            request_id = uuid.uuid4().hex
            expected = dict(current)
            expected["task_cron"] = []
            source.persist_task_link_request({
                "request_id": request_id,
                "script_id": current["id"],
                "proxy_id": "",
                "expected_version": source.online_task_link_version(current),
                "state": "prepared" if prepared else "unknown",
                "added": 0,
                "total": len(current.get("task_cron") or []),
                "error": "fixture interrupted before merge",
                "updated_at": "old",
                "expected_tasks_fingerprint": source.online_script_task_fingerprint(expected),
                "published_tasks_fingerprint": "",
                "confirmation": "",
                "confirmation_fingerprint": "",
            })
            return jsonify(request_id=request_id, script_id=current["id"])

        @app.route("/api/fixture/task-link-history", methods=["POST"])
        def task_link_history_fixture():
            mode = (request.get_json() or {}).get("mode")
            request_id = "0" * 32
            record = {
                "request_id": request_id, "script_id": item["id"], "proxy_id": "",
                "expected_version": source.online_task_link_version(source.normalize_online_scripts([item])[0]),
                "state": "unknown", "added": 0, "total": 0, "error": "history original unknown diagnosis",
                "updated_at": "2000-01-01 00:00:00", "expected_tasks_fingerprint": "1" * 64,
                "published_tasks_fingerprint": "", "confirmation": "", "confirmation_fingerprint": "",
            }
            records = {}
            constants.ONLINE_TASK_LINK_ACTIVE.clear()
            if mode in ("full", "prune"):
                records = {f"{index:032x}": source._task_link_request_snapshot(dict(
                    record, request_id=f"{index:032x}", updated_at="2099-01-01 00:00:00",
                    state="unknown" if mode == "full" else "completed",
                )) for index in range(200)}
                records[request_id] = source._task_link_request_snapshot(record)
            if mode == "active_missing":
                constants.ONLINE_TASK_LINK_ACTIVE.add(request_id)
            if mode == "restore_active":
                records[request_id] = source._task_link_request_snapshot(dict(record, state="completed"))
            source.mutate_json(source.ONLINE_TASK_LINK_REQUESTS_FILE, {}, lambda _: records, strict=True)
            if mode == "prune":
                source.persist_task_link_request(dict(record, request_id="f" * 32, state="completed"))
            return jsonify(request_id=request_id, count=len(source.list_task_link_requests(100)), fetches=fault["fetches"])

        @app.route("/api/fixture/refresh-retry", methods=["POST"])
        def refresh_retry_fixture():
            fault["refreshes"] = 0
            data = request.get_json() or {}
            fault["fail_retry_start"] = bool(data.get("fail_start"))
            fault["fail_retry_receipt"] = bool(data.get("fail_receipt"))
            original = uuid.uuid4().hex
            refresh_id = uuid.uuid4().hex
            path = online_refresh_log_file(refresh_id)
            path.write_text("original refresh failed\n", encoding="utf-8")
            update_online_refresh_state(
                id=refresh_id, request_id=original, proxy_id="", running=False,
                status="失败", error="fixture retry needed", message="", task_link_failures=[],
                task_link_failure_count=0,
                task_link_failure_digest=constants._refresh_task_failure_digest([]),
                cache_published=False, expected_cache_fingerprint=source.online_script_cache_fingerprint(),
                published_cache_fingerprint="", source_fingerprint=source.online_script_source_fingerprint(),
                retry_of="", confirmation="", confirmation_fingerprint="",
                log_file=str(path), updated_at="zz-original",
            )
            return jsonify(ok=True, original=original)

        @app.route("/api/fixture/refresh-history-capacity", methods=["POST"])
        def refresh_history_capacity_fixture():
            fault["refreshes"] = 0
            cache_fingerprint = source.online_script_cache_fingerprint()
            source_fingerprint = source.online_script_source_fingerprint()
            records = {}
            previous_id = ""
            for number in range(1, constants.ONLINE_REFRESH_HISTORY_LIMIT + 1):
                request_id = f"{number:032x}"
                status = "失败" if number == constants.ONLINE_REFRESH_HISTORY_LIMIT else "已完成"
                records[request_id] = {
                    "id": request_id,
                    "request_id": request_id,
                    "proxy_id": "",
                    "running": False,
                    "status": status,
                    "message": "",
                    "error": "upstream timeout" if status == "失败" else "",
                    "cache_published": status == "已完成",
                    "task_link_failures": [],
                    "updated_at": f"2026-01-{number:03d}",
                    "log_file": "",
                    "retry_of": previous_id,
                    "expected_cache_fingerprint": cache_fingerprint,
                    "published_cache_fingerprint": cache_fingerprint if status == "已完成" else "",
                    "source_fingerprint": source_fingerprint,
                    "confirmation": "",
                    "confirmation_fingerprint": "",
                }
                previous_id = request_id
            update_online_refresh_state(
                id="", request_id="", proxy_id="", running=False, status="", message="", error="",
                cache_published=False, task_link_failures=[], updated_at="", log_file="",
            )
            constants.ONLINE_REFRESH_HISTORY_FILE.write_text(json.dumps(records), encoding="utf-8")
            update_online_refresh_state(**records[previous_id])
            return jsonify(original=previous_id, count=len(records))

        @app.route("/api/fixture/refresh-history-change", methods=["POST"])
        def refresh_history_change_fixture():
            update_online_refresh_state(status="部分完成", message="history changed fixture", updated_at="zz-history-change")
            return jsonify(ok=True)

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

        @app.route("/api/fixture/orphaned-refresh", methods=["POST"])
        def orphaned_refresh_fixture():
            fault["refreshes"] = 0
            original, refresh_id = uuid.uuid4().hex, uuid.uuid4().hex
            path = online_refresh_log_file(refresh_id)
            path.write_text("orphaned original refresh log\n", encoding="utf-8")
            update_online_refresh_state(
                id=refresh_id, request_id=original, proxy_id="", running=True,
                status="运行中", error="original orphan diagnosis", message="", task_link_failures=[],
                cache_published=False, expected_cache_fingerprint=source.online_script_cache_fingerprint(),
                published_cache_fingerprint="", source_fingerprint=source.online_script_source_fingerprint(),
                retry_of="", confirmation="", confirmation_fingerprint="", log_file=str(path), updated_at="old",
            )
            update_online_refresh_state(
                id=uuid.uuid4().hex, request_id=uuid.uuid4().hex, running=False,
                status="已完成", error="", message="newer refresh finished", log_file="", updated_at="new",
            )
            for index in range(10):
                update_online_refresh_state(
                    id=uuid.uuid4().hex, request_id=uuid.uuid4().hex,
                    running=False, status="已完成", updated_at=f"2099-01-01 00:00:{index:02d}",
                )
            constants.ONLINE_REFRESH_STATE.clear()
            return jsonify(original=original)

        @app.route("/api/fixture/refresh-log-file", methods=["POST"])
        def refresh_log_file_fixture():
            data = request.get_json() or {}
            info = constants.get_online_refresh_request(data.get("request_id"))
            if not info:
                return jsonify(error="fixture refresh missing"), 404
            path = online_refresh_log_file(info["id"])
            if data.get("restore"):
                path.write_text("restored original refresh log\n", encoding="utf-8")
            else:
                path.unlink(missing_ok=True)
            return jsonify(refreshes=fault["refreshes"])

        delayed_refresh = {}

        @app.route("/api/fixture/delayed-refresh-log", methods=["POST"])
        def delayed_refresh_log_fixture():
            data = request.get_json() or {}
            action = data.get("action")
            if action == "seed":
                refresh_id, request_id = uuid.uuid4().hex, uuid.uuid4().hex
                log_file = online_refresh_log_file(refresh_id)
                log_file.unlink(missing_ok=True)
                delayed_refresh.update(id=refresh_id, request_id=request_id, log_file=log_file)
                update_online_refresh_state(
                    id=refresh_id, request_id=request_id, proxy_id="", running=True, status="运行中",
                    message="正在后台同步在线脚本源", error="", cache_published=False,
                    task_link_failures=[], task_link_failure_count=0,
                    task_link_failure_digest=constants._refresh_task_failure_digest([]),
                    updated_at="delayed-log-start", log_file=str(log_file),
                )
            elif action in ("publish", "complete", "remove") and delayed_refresh:
                log_file = delayed_refresh["log_file"]
                if action == "publish":
                    log_file.write_text("delayed refresh log available\n", encoding="utf-8")
                elif action == "complete":
                    update_online_refresh_state(
                        running=False, status="已完成", message="fixture refresh completed",
                        error="", cache_published=True, updated_at="delayed-log-complete",
                    )
                else:
                    log_file.unlink(missing_ok=True)
            else:
                return jsonify(ok=False, error="invalid delayed refresh action"), 400
            return jsonify(
                ok=True, id=delayed_refresh.get("id"), request_id=delayed_refresh.get("request_id"),
                running=bool(constants.get_online_refresh_state().get("running")),
                refreshes=fault["refreshes"],
            )

        server = make_server("127.0.0.1", 0, app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            env = dict(os.environ, FLS_UI_BASE=f"http://127.0.0.1:{server.server_port}",
                       FLS_UI_TOKEN=TOKEN, FLS_PLAYWRIGHT_CORE=str(playwright), FLS_CHROMIUM=str(chromium))
            subprocess.run(["node", "-e", SCRIPT], env=env, check=True, timeout=180)
        finally:
            task_link.fetch_task_link_tasks = real_fetch
            task_link.persist_task_link_request = real_persist
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
  const paginationWidth=Number(process.env.FLS_REFRESH_HISTORY_PAGINATION_VIEWPORT||0);
  const viewports=paginationWidth===390?[{width:390,height:844}]:paginationWidth===1280?[{width:1280,height:800}]:[{width:390,height:844},{width:1280,height:800}];
  for(const viewport of viewports){
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
   const refreshRecoveryId=('c'+viewport.width.toString(16)).padEnd(32,'c');
   const refreshRecoveryRequest=('b'+viewport.width.toString(16)).padEnd(32,'b');
   await fixture({reset:true,fail:true});
   const refreshSetup=await page.request.post(base+'/api/fixture/online-refresh',{headers,data:{
     failure_count:105,request_id:refreshRecoveryRequest,refresh_id:refreshRecoveryId,
   }});
   assert.equal(refreshSetup.status(),200);
   const refreshFailureUrl=base+'/online-scripts?refresh_request_id='+refreshRecoveryRequest+'&failure_offset=100#online-status';
   await page.goto(refreshFailureUrl);
   const recoveredFailure=page.locator('#onlineRefreshTaskLinkFailures form[data-script-id="browser-link-104"]');
   await recoveredFailure.waitFor();
   const stableRecoveryRequest=await recoveredFailure.locator('[name="request_id"]').inputValue();
   assert.match(stableRecoveryRequest,/^[a-f0-9]{32}$/);
   await page.reload();
   assert.equal(await recoveredFailure.locator('[name="request_id"]').inputValue(),stableRecoveryRequest);
   await fixture({fail:false});
   const [recoveredResponse]=await Promise.all([
     page.waitForResponse(response=>response.url().includes('/api/online-scripts/refresh-task-link/'+refreshRecoveryRequest+'/task-link/browser-link-104')&&response.request().method()==='POST'),
     recoveredFailure.locator('button[type="submit"]').click(),
   ]);
   assert.equal(recoveredResponse.status(),200);
   await page.waitForURL(url=>url.searchParams.get('msg')?.includes('外部任务源同步完成'));
   assert.equal((await fixture({})).fetches,1);
   const damagePrimary=await page.request.post(base+'/api/fixture/refresh-recovery-damage',{headers,data:{request_id:refreshRecoveryRequest}});
   assert.equal(damagePrimary.status(),200);
   const repairedStatus=await page.request.get(base+'/api/online-scripts/refresh-status/'+refreshRecoveryRequest,{headers});
   assert.equal(repairedStatus.status(),200);
   assert.equal((await repairedStatus.json()).task_link_recovery_maintenance.repaired,true);
   const damageBackup=await page.request.post(base+'/api/fixture/refresh-recovery-damage',{headers,data:{request_id:refreshRecoveryRequest,copy:'backup'}});
   assert.equal(damageBackup.status(),200);
   await page.goto(refreshFailureUrl);
   await page.locator('#onlineRefreshTaskLinkFailures').getByText(/恢复回执副本已自动修复/).waitFor();
   await page.locator('#onlineRefreshTaskLinkFailures').getByText(/浏览器任务源 104：已恢复/).waitFor();
   assert.equal(await page.locator('#onlineRefreshTaskLinkFailures form[data-script-id="browser-link-104"]').count(),0);
   const recoveredStatus=await page.request.get(base+'/api/online-scripts/refresh-task-link-failures/'+refreshRecoveryRequest+'?offset=100&limit=100',{headers});
   assert.equal(recoveredStatus.status(),200);
   assert.equal((await recoveredStatus.json()).items[4].recovery.state,'completed');
   assert.equal((await fixture({})).fetches,1);
   await page.screenshot({path:'/tmp/fls-refresh-failure-page-'+viewport.width+'.png'});
   if(process.env.FLS_REFRESH_RECOVERY_ONLY==='1'){
     assert.deepEqual(errors,[]);
     console.log('online refresh failure recovery passed '+viewport.width);
     await context.close();
     continue;
   }
   const historyFixture=async mode=>{
     const response=await page.request.post(base+'/api/fixture/task-link-history',{headers,data:{mode}});
     assert.equal(response.status(),200);return response.json();
   };
   await fixture({reset:true,fail:false});
   const pruned=await historyFixture('prune');
   await page.goto(base+'/online-scripts/task-link-history?recovery=1');
   await page.locator('a[href="/online-scripts/task-link-status/'+pruned.request_id+'"]').click();
   await page.waitForURL(base+'/online-scripts/task-link-status/'+pruned.request_id);
   assert.match(await page.locator('.content').textContent(),/history original unknown diagnosis/);
   assert.equal((await fixture({})).fetches,0);
   await historyFixture('full');
   await page.goto(base+'/online-scripts');
   const fullForm=page.locator('#online-list form[action="/online-scripts/task-link/browser-link"]');
   await fullForm.locator('button[type="submit"]').click();
   await page.getByRole('link',{name:'处理待恢复同步',exact:true}).waitFor();
   assert.equal(await fullForm.locator('button[type="submit"]').isEnabled(),true);
   assert.equal(await page.evaluate(()=>sessionStorage.getItem('fls-online-task-link-request')),null);
   assert.equal((await fixture({})).fetches,0);
   await page.getByRole('link',{name:'处理待恢复同步',exact:true}).click();
   await page.waitForURL(base+'/online-scripts/task-link-history?recovery=1');
   await page.getByRole('link',{name:'下一页',exact:true}).click();
   await page.waitForURL(url=>url.searchParams.get('offset')==='50');
   assert.ok(new URL(page.url()).searchParams.get('offset')==='50');
   assert.ok(new URL(page.url()).searchParams.get('recovery')==='1');
   await page.locator('a[href*="/online-scripts/task-link-status/"]').first().click();
   await page.waitForURL(url=>url.pathname.startsWith('/online-scripts/task-link-status/'));
   const archiveId=new URL(page.url()).pathname.split('/').pop();
   const archiveForm=page.locator('form[action="/online-scripts/task-link-archive/'+archiveId+'"]');
   await archiveForm.locator('[name="acknowledge"]').check();
   const archiveLabelBox=await archiveForm.locator('label').boundingBox();
   const archiveButtonBox=await archiveForm.locator('button').boundingBox();
   assert.ok(archiveLabelBox.y+archiveLabelBox.height<=archiveButtonBox.y);
   assert.ok(archiveLabelBox.x>=0&&archiveLabelBox.x+archiveLabelBox.width<=viewport.width);
   assert.ok(archiveButtonBox.x>=0&&archiveButtonBox.x+archiveButtonBox.width<=viewport.width);
   await page.screenshot({path:'/tmp/fls-task-link-archive-'+viewport.width+'.png'});
   const archivePost=base+'/online-scripts/task-link-archive/'+archiveId;
   await page.route(archivePost,async route=>{
     const response=await route.fetch({maxRedirects:0});assert.equal(response.status(),302);
     await route.fulfill({status:503,contentType:'text/html',body:'archive response lost after publication'});
   });
   await archiveForm.getByRole('button',{name:'归档原同步请求',exact:true}).click();
   await page.waitForFunction(()=>document.body.textContent.includes('archive response lost after publication'));
   await page.unroute(archivePost);
   await page.goto(base+'/online-scripts/task-link-status/'+archiveId);
   await page.getByRole('heading',{name:'已归档（原结果：结果未知）',exact:true}).waitFor();
   assert.equal(await page.getByRole('button',{name:'继续原同步请求',exact:true}).count(),0);
   assert.equal(await page.getByRole('button',{name:'检查当前任务合并',exact:true}).count(),0);
   assert.equal((await fixture({})).fetches,0);
   await page.evaluate(id=>sessionStorage.setItem('fls-online-task-link-request',JSON.stringify({request_id:id,script_id:'browser-link',proxy_id:''})),archiveId);
   await page.goto(base+'/online-scripts');
   await page.waitForURL(base+'/online-scripts/task-link-status/'+archiveId);
   assert.equal(await page.evaluate(()=>sessionStorage.getItem('fls-online-task-link-request')),null);
   await page.getByRole('link',{name:'核对当前任务',exact:true}).click();
   await page.waitForURL(base+'/online-scripts');
   const acceptedSync=page.waitForResponse(response=>response.url()===base+'/api/online-scripts/task-link/browser-link'&&response.request().method()==='POST');
   await page.locator('#online-list form[action="/online-scripts/task-link/browser-link"] button[type="submit"]').click();
   const acceptedResponse=await acceptedSync;
   assert.equal(acceptedResponse.status(),200);
   await page.waitForURL(url=>url.pathname==='/online-scripts'&&url.searchParams.has('msg'));
   const acceptedState=await fixture({});
   assert.equal(acceptedState.fetches,1);
   assert.equal(acceptedState.cached[0].task_cron[0].name,'Browser task');
   await page.screenshot({path:'/tmp/fls-task-link-history-'+viewport.width+'.png'});
   const missing=await historyFixture('active_missing');
   await page.evaluate(id=>sessionStorage.setItem('fls-online-task-link-request',JSON.stringify({request_id:id,script_id:'browser-link',proxy_id:''})),missing.request_id);
   await page.goto(base+'/online-scripts');
   await page.waitForFunction(()=>document.querySelector('#onlineTaskLinkRecovery').textContent.includes('回执暂时缺失'));
   assert.equal(await page.getByRole('button',{name:'继续原同步请求',exact:true}).count(),0);
   assert.equal((await fixture({})).fetches,1);
   await historyFixture('restore_active');
   await page.getByRole('button',{name:'重新读取同步状态',exact:true}).click();
   await page.waitForURL(base+'/online-scripts/task-link-status/'+missing.request_id);
   assert.equal(await page.evaluate(()=>sessionStorage.getItem('fls-online-task-link-request')),null);
   assert.equal((await fixture({})).fetches,1);
   await historyFixture('clear');
   console.log('task-link protected history, full-capacity archive and missing receipt recovery passed '+viewport.width);
   if(process.env.FLS_TASK_LINK_HISTORY_ONLY==='1'){
     assert.deepEqual(errors,[]);await context.close();continue;
   }
   await fixture({reset:true,fail:false});
   const preparedFixture=await page.request.post(base+'/api/fixture/task-link-confirmation',{headers,data:{prepared:true}});
   assert.equal(preparedFixture.status(),200);
   const preparedInfo=await preparedFixture.json();
   await page.evaluate(info=>sessionStorage.setItem('fls-online-task-link-request',JSON.stringify({request_id:info.request_id,script_id:info.script_id,proxy_id:''})),preparedInfo);
   const preparedStatus=base+'/api/online-scripts/task-link-status/'+preparedInfo.request_id;
   await page.route(preparedStatus,route=>route.fulfill({status:503,contentType:'text/plain',body:'startup state temporarily unavailable'}));
   await page.goto(base+'/online-scripts');
   await page.waitForFunction(()=>document.querySelector('#onlineTaskLinkRecovery').textContent.includes('startup state temporarily unavailable'));
   assert.equal(await page.getByRole('button',{name:'继续原同步请求',exact:true}).count(),0);
   assert.equal(await page.evaluate(()=>JSON.parse(sessionStorage.getItem('fls-online-task-link-request')).request_id),preparedInfo.request_id);
   await page.unroute(preparedStatus);
   await page.getByRole('button',{name:'重新读取同步状态',exact:true}).click();
   await page.getByRole('button',{name:'继续原同步请求',exact:true}).waitFor();
   assert.match(await page.locator('#onlineTaskLinkRecovery').textContent(),/未发起外部请求/);
   assert.equal((await fixture({})).fetches,0);
   await page.getByRole('link',{name:'查询原同步请求',exact:true}).click();
   await page.waitForURL(base+'/online-scripts/task-link-status/'+preparedInfo.request_id);
   const preparedRetry=page.locator('form[action="/online-scripts/task-link/browser-link"]');
   assert.equal(await preparedRetry.locator('[name="request_id"]').inputValue(),preparedInfo.request_id);
   await page.screenshot({path:'/tmp/fls-task-link-prepared-'+viewport.width+'.png'});
   await preparedRetry.locator('button[type="submit"]').click();
   await page.getByRole('heading',{name:'同步已完成',exact:true}).waitFor();
   await page.waitForURL(base+'/online-scripts/task-link-status/'+preparedInfo.request_id);
   assert.match(await page.locator('.content').textContent(),/同步已完成/);
   assert.equal(await page.evaluate(()=>sessionStorage.getItem('fls-online-task-link-request')),null);
   assert.equal((await fixture({})).fetches,1);
   await page.goto(base+'/online-scripts');
   await page.locator('#online-list').waitFor();
   assert.equal((await fixture({})).fetches,1);

   for(const mode of ['before','after']){
     await fixture({reset:true,fail:false,startup_failure:mode});
     await page.goto(base+'/online-scripts');
     const startupForm=page.locator('#online-list form[action="/online-scripts/task-link/browser-link"]');
     const startupId=await startupForm.locator('[name="request_id"]').inputValue();
     const startupPost=base+'/api/online-scripts/task-link/browser-link';
     await page.route(startupPost,async route=>{const response=await route.fetch();assert.equal(response.status(),500);await route.abort('connectionfailed');});
     await startupForm.locator('button[type="submit"]').click();
     await page.waitForFunction(()=>document.querySelector('#onlineTaskLinkRecovery').textContent.includes('外部任务源同步失败'));
     assert.equal((await fixture({})).fetches,0);
     assert.equal(await page.evaluate(()=>JSON.parse(sessionStorage.getItem('fls-online-task-link-request')).request_id),startupId);
     await page.unroute(startupPost);
     await page.reload();
     await page.getByRole('button',{name:'继续原同步请求',exact:true}).waitFor();
     assert.match(await page.locator('#onlineTaskLinkRecovery').textContent(),/browser startup receipt unavailable/);
     await page.getByRole('button',{name:'继续原同步请求',exact:true}).click();
     await page.waitForURL(url=>url.pathname==='/online-scripts'&&(url.searchParams.get('msg')||'').includes('外部任务源同步完成'));
     assert.equal((await fixture({})).fetches,1);
     const replay=await page.request.post(startupPost,{headers,data:{request_id:startupId}});
     assert.equal(replay.status(),200);assert.equal((await replay.json()).replayed,true);
     assert.equal((await fixture({})).fetches,1);
   }
   console.log('prepared/startup failure recovery passed '+viewport.width);
   if(process.env.FLS_TASK_LINK_STARTUP_ONLY==='1'){
     assert.deepEqual(errors,[]);await context.close();continue;
   }
   await fixture({reset:true,fail:true});
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
   await page.waitForURL(url=>url.pathname==='/online-scripts'&&(url.searchParams.get('msg')||'').includes('外部任务源同步完成'));
   const state=await fixture({});
   assert.equal(state.fetches,2);
   assert.equal(state.cached[0].task_cron[0].name,'Browser task');

   await fixture({reset:true,fail:false});
   await page.goto(base+'/online-scripts');
   const lostForm=page.locator('#online-list form[action="/online-scripts/task-link/browser-link"]');
   const lostId=await lostForm.locator('[name="request_id"]').inputValue();
   const taskPost=base+'/api/online-scripts/task-link/browser-link';
   await page.route(taskPost,async route=>{
     const response=await route.fetch();
     assert.equal(response.status(),200);
     assert.equal((await response.json()).request_id,lostId);
     const failure=await page.request.post(base+'/api/fixture/task-link-cache',{headers,data:{}});
     assert.equal(failure.status(),200);
     assert.equal((await failure.json()).fetches,1);
     await route.abort('connectionfailed');
   });
   await lostForm.locator('button[type="submit"]').click();
   await page.waitForFunction(()=>document.querySelector('#onlineTaskLinkRecovery').textContent.includes('外部任务源同步失败'));
   assert.equal(await page.getByRole('button',{name:'继续原同步请求',exact:true}).count(),0);
   assert.equal(await lostForm.locator('button[type="submit"]').isEnabled(),true);
   assert.equal(await page.evaluate(()=>JSON.parse(sessionStorage.getItem('fls-online-task-link-request')).request_id),lostId);
   await page.unroute(taskPost);
   await lostForm.locator('button[type="submit"]').click();
   await page.waitForURL(base+'/online-scripts/task-link-status/'+lostId);
   assert.match(await page.locator('.content').textContent(),/同步已完成/);
   assert.equal(await page.evaluate(()=>sessionStorage.getItem('fls-online-task-link-request')),null);
   await page.evaluate(id=>sessionStorage.setItem('fls-online-task-link-request',JSON.stringify({request_id:id,script_id:'browser-link',proxy_id:''})),lostId);
   await page.goto(base+'/online-scripts');
   await page.waitForURL(base+'/online-scripts/task-link-status/'+lostId);
   assert.match(await page.locator('.content').textContent(),/同步已完成/);
   await page.screenshot({path:'/tmp/fls-task-link-replay-'+viewport.width+'.png'});
   const restoredTaskLinkCache=await page.request.post(base+'/api/fixture/task-link-cache',{headers,data:{restore:true}});
   assert.equal(restoredTaskLinkCache.status(),200);
   assert.equal((await restoredTaskLinkCache.json()).fetches,1);

   await fixture({reset:true,fail:false});
   const seededConfirmation=await page.request.post(base+'/api/fixture/task-link-confirmation',{headers,data:{}});
   assert.equal(seededConfirmation.status(),200);
   const confirmationInfo=await seededConfirmation.json();
   await page.evaluate(info=>sessionStorage.setItem('fls-online-task-link-request',JSON.stringify({request_id:info.request_id,script_id:info.script_id,proxy_id:''})),confirmationInfo);
   await page.goto(base+'/online-scripts');
   await page.getByRole('button',{name:'检查当前任务合并',exact:true}).waitFor();
   await page.getByRole('button',{name:'检查当前任务合并',exact:true}).click();
   await page.getByRole('button',{name:'继续原同步请求',exact:true}).waitFor();
   const changedCache=await page.request.post(base+'/api/fixture/task-link-cache',{headers,data:{add_task:true}});
   assert.equal(changedCache.status(),200);
   await page.getByRole('button',{name:'继续原同步请求',exact:true}).click();
   await page.waitForFunction(()=>document.querySelector('#onlineTaskLinkRecovery').textContent.includes('确认已因任务缓存变化失效'));
   assert.equal(await page.getByRole('button',{name:'继续原同步请求',exact:true}).count(),0);
   assert.equal(await page.getByRole('button',{name:'检查当前任务合并',exact:true}).count(),0);
   await page.getByRole('button',{name:'重新读取同步状态',exact:true}).click();
   await page.waitForFunction(()=>document.querySelector('#onlineTaskLinkRecovery').textContent.includes('确认已因任务缓存变化失效'));
   await page.getByRole('link',{name:'查询原同步请求',exact:true}).click();
   await page.waitForURL(base+'/online-scripts/task-link-status/'+confirmationInfo.request_id);
   const staleConfirmationStatus=page.locator('.content');
   assert.match(await staleConfirmationStatus.textContent(),/原未合并确认已失效，任务缓存已变化/);
   assert.equal(await staleConfirmationStatus.locator('form[action="/online-scripts/task-link/browser-link"]').count(),0);
   assert.equal(await page.getByRole('link',{name:'核对当前任务',exact:true}).count(),1);
   await page.screenshot({path:'/tmp/fls-task-link-stale-confirmation-'+viewport.width+'.png'});
   await page.evaluate(()=>sessionStorage.removeItem('fls-online-task-link-request'));

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
   await page.goto(base+'/online-scripts?refresh_request_id='+'d'.repeat(32));
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
   await page.waitForURL(url=>url.pathname==='/online-scripts'&&(url.searchParams.get('msg')||'').includes('外部任务源同步完成'));
   const dynamicState=await fixture({});
   assert.equal(dynamicState.fetches,2);

   const autoRetryRequest=('a'+viewport.width.toString(16)).padEnd(32,'a');
   const autoRetryRefresh=('b'+viewport.width.toString(16)).padEnd(32,'b');
   const runningFixture=await page.request.post(base+'/api/fixture/online-refresh',{headers,data:{
     running:true,request_id:autoRetryRequest,refresh_id:autoRetryRefresh,
   }});
   assert.equal(runningFixture.status(),200);
   await page.evaluate(({requestId})=>sessionStorage.setItem('fls-online-refresh-request',JSON.stringify({
     request_id:requestId,proxy_id:'',server_id:'',
   })),{requestId:autoRetryRequest});
   const autoStatusUrl=base+'/api/online-scripts/refresh-status/'+autoRetryRequest;
   let autoStatusReads=0,refreshPosts=0;
   const countRefreshPosts=request=>{
     if(request.method()==='POST'&&request.url().includes('/api/online-scripts/refresh')) refreshPosts++;
   };
   page.on('request',countRefreshPosts);
   await page.route(autoStatusUrl,route=>{
     autoStatusReads++;
     if(autoStatusReads===1) return route.fulfill({status:503,contentType:'text/html',body:'temporary status unavailable'});
     return route.continue();
   });
   await page.goto(base+'/online-scripts');
   await page.waitForFunction(()=>document.querySelector('#onlineRefreshStatusText').textContent.includes('1/5'));
   assert.equal(await page.locator('#onlineRefreshQuery').isVisible(),true);
   await page.screenshot({path:'/tmp/fls-refresh-status-auto-retry-'+viewport.width+'.png'});
   const recoveredStatusResponse=page.waitForResponse(response=>response.url()===autoStatusUrl&&response.status()===200,{timeout:7000});
   await recoveredStatusResponse;
   await page.waitForFunction(()=>!document.querySelector('#onlineRefreshStatusText').textContent.includes('HTTP 503'));
   assert.equal(autoStatusReads,2);
   assert.equal(refreshPosts,0,'automatic status recovery must not repeat the synchronization POST');
   await page.evaluate(()=>window.dispatchEvent(new Event('pagehide')));
   await page.waitForTimeout(2200);
   assert.equal(autoStatusReads,2,'pagehide clears the next poll timer');
   assert.deepEqual(errors,[]);
   page.off('request',countRefreshPosts);
   await page.unroute(autoStatusUrl);

   const historyRequestId=('c'+viewport.width.toString(16)).padEnd(32,'c');
   const historyRefreshId=('d'+viewport.width.toString(16)).padEnd(32,'d');
   await fixture({reset:true,fail:true});
   const refreshHistoryFixture=await page.request.post(base+'/api/fixture/online-refresh',{headers,data:{
     running:false,request_id:historyRequestId,refresh_id:historyRefreshId,
   }});
   assert.equal(refreshHistoryFixture.status(),200);
   await page.evaluate(()=>sessionStorage.removeItem('fls-online-refresh-request'));
   const historyUrl=base+'/api/online-scripts/refresh-history';
   let historyReads=0,historyRefreshPosts=0;
   const countHistoryRefreshPosts=request=>{
     if(request.method()==='POST'&&request.url().includes('/api/online-scripts/refresh')) historyRefreshPosts++;
   };
   page.on('request',countHistoryRefreshPosts);
   await page.route('**/api/online-scripts/refresh-history*',route=>{
     historyReads++;
     if(historyReads===1) return route.fulfill({status:503,contentType:'text/html',body:'history gateway unavailable'});
     if(historyReads===2) return route.fulfill({status:401,contentType:'application/json',body:JSON.stringify({error:'login expired'})});
     return route.continue();
   });
   await page.goto(base+'/online-scripts');
   const historyRow=page.locator('#onlineRefreshHistory a[href="/online-scripts/refresh-log/'+historyRequestId+'"]');
   await historyRow.waitFor();
   await page.waitForFunction(()=>document.querySelector('#onlineRefreshHistoryError').textContent.includes('HTTP 503'));
   assert.equal(await page.locator('#onlineRefreshHistoryRetry').isVisible(),true);
   assert.equal(await page.locator('#onlineRefreshHistoryLogin').isVisible(),false);
   await page.screenshot({path:'/tmp/fls-refresh-history-retry-'+viewport.width+'.png'});
   await page.locator('#onlineRefreshHistoryRetry').click();
   await page.waitForFunction(()=>document.querySelector('#onlineRefreshHistoryError').textContent.includes('HTTP 401'));
   assert.equal(await page.locator('#onlineRefreshHistoryLogin').isVisible(),true);
   assert.equal(await page.locator('#onlineRefreshHistoryLogin').getAttribute('href'),'/login?next=%2Fonline-scripts');
   assert.equal(await historyRow.count(),1,'failed history reads must preserve the server-rendered row');
   const [historyRecovered]=await Promise.all([
     page.waitForResponse(response=>response.url().startsWith(historyUrl)&&response.status()===200),
     page.locator('#onlineRefreshHistoryRetry').click(),
   ]);
   assert.equal(historyRecovered.status(),200);
   await page.waitForFunction(()=>document.querySelector('#onlineRefreshHistoryError').hidden);
   assert.equal(await historyRow.count(),1);
   assert.equal(await page.locator('#onlineRefreshHistoryRetry').isVisible(),false);
   assert.equal(await page.locator('#onlineRefreshHistoryLogin').isVisible(),false);
   assert.equal(historyReads,3);
   assert.equal(historyRefreshPosts,0,'history recovery must not submit a refresh or task-link request');
   page.off('request',countHistoryRefreshPosts);
   await page.unroute('**/api/online-scripts/refresh-history*');

   const manyFailureRequestId='9'.repeat(32);
   await fixture({reset:true,fail:true});
   const manyFailures=await page.request.post(base+'/api/fixture/online-refresh',{headers,data:{failure_count:105,request_id:manyFailureRequestId,refresh_id:'8'.repeat(32)}});
   assert.equal(manyFailures.status(),200);
   const failurePageApi=await page.request.get(base+'/api/online-scripts/refresh-task-link-failures/'+manyFailureRequestId+'?offset=100&limit=100',{headers});
   assert.equal(failurePageApi.status(),200);
   const failurePagePayload=await failurePageApi.json();
   assert.equal(failurePagePayload.total,105);
   assert.equal(failurePagePayload.items.length,5);
   assert.equal(failurePagePayload.items[4].script_id,'browser-link-104');
   await page.goto(base+'/online-scripts?refresh_request_id='+manyFailureRequestId+'&failure_offset=100#online-status');
   const lastFailure=page.locator('#onlineRefreshTaskLinkFailures form[data-script-id="browser-link-104"]');
   await lastFailure.waitFor();
   assert.match(await page.locator('#onlineRefreshTaskLinkFailures').textContent(),/失败源 101-105 \/ 105/);
   assert.equal(await lastFailure.getAttribute('action'),'/online-scripts/refresh-task-link/'+manyFailureRequestId+'/task-link/browser-link-104');
   const lastFailureRequestId=await lastFailure.locator('[name="request_id"]').inputValue();
   await page.reload();
   assert.equal(await lastFailure.locator('[name="request_id"]').inputValue(),lastFailureRequestId);
   const [lastFailureResponse]=await Promise.all([
     page.waitForResponse(response=>response.url().includes('/api/online-scripts/refresh-task-link/'+manyFailureRequestId+'/task-link/browser-link-104')&&response.request().method()==='POST'),
     lastFailure.locator('button[type="submit"]').click(),
   ]);
   assert.equal(lastFailureResponse.status(),502);
   const failurePending=JSON.parse(await page.evaluate(()=>sessionStorage.getItem('fls-online-task-link-request')));
   assert.equal(failurePending.request_id,lastFailureRequestId);
   assert.equal(failurePending.origin_refresh_request_id,manyFailureRequestId);
   assert.equal((await fixture({})).fetches,1);
   const continueLastFailure=page.getByRole('button',{name:'继续原同步请求',exact:true});
   await continueLastFailure.waitFor();
   await fixture({fail:false});
   await continueLastFailure.click();
   await page.waitForURL(url=>url.pathname==='/online-scripts'&&(url.searchParams.get('msg')||'').includes('外部任务源同步完成'));
   const boundReceipt=await page.request.get(base+'/api/online-scripts/task-link-status/'+lastFailureRequestId,{headers});
   assert.equal(boundReceipt.status(),200);
   assert.equal((await boundReceipt.json()).origin_refresh_request_id,manyFailureRequestId);
   assert.equal((await fixture({})).fetches,2);
   await page.goto(base+'/online-scripts?refresh_request_id='+manyFailureRequestId+'&failure_offset=100#online-status');
   await page.locator('#onlineRefreshTaskLinkFailures').getByText(/浏览器任务源 104：已恢复/).waitFor();
   assert.match(await page.locator('#onlineRefreshTaskLinkFailures').textContent(),/已恢复/);
   assert.equal(await page.locator('#onlineRefreshTaskLinkFailures form[data-script-id="browser-link-104"]').count(),0);
   assert.equal((await fixture({})).fetches,2);
   await page.screenshot({path:'/tmp/fls-refresh-failure-page-'+viewport.width+'.png'});
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

   const failedRetrySetup=await page.request.post(base+'/api/fixture/refresh-retry',{headers,data:{fail_start:true,fail_receipt:true}});
   assert.equal(failedRetrySetup.status(),200);
   const failedRetryOriginal=(await failedRetrySetup.json()).original;
   await page.goto(base+'/online-scripts');
   const failedRetryForm=page.locator('#onlineRefreshHistory form[data-refresh-retry-of="'+failedRetryOriginal+'"]');
   await failedRetryForm.waitFor();
   const failedRetryEndpoint=base+'/api/online-scripts/refresh-retry/'+failedRetryOriginal;
   const [failedRetryResponse]=await Promise.all([
     page.waitForResponse(response=>response.url()===failedRetryEndpoint&&response.request().method()==='POST'),
     failedRetryForm.getByRole('button',{name:'明确重新同步',exact:true}).click(),
   ]);
   assert.equal(failedRetryResponse.status(),500);
   const failedRetryId=(await failedRetryResponse.json()).request_id;
   assert.match(failedRetryId,/^[a-f0-9]{32}$/);
   await page.waitForFunction(()=>document.querySelector('#onlineRefreshHistoryError').textContent.includes('browser retry thread unavailable'));
   assert.equal(await page.evaluate(id=>JSON.parse(sessionStorage.getItem('fls-online-refresh-request')).request_id,failedRetryId),failedRetryId);
   const failedRetryState=await page.request.get(base+'/api/online-scripts/refresh-status/'+failedRetryId,{headers});
   assert.equal(failedRetryState.status(),200);
   assert.equal((await failedRetryState.json()).running,false);
   assert.equal((await failedRetryState.json()).status,'启动失败');
   assert.match((await failedRetryState.json()).persistence_error,/browser retry status disk full/);
   await page.screenshot({path:'/tmp/fls-refresh-retry-start-failure-'+viewport.width+'.png'});
   await page.locator('#onlineRefreshQuery').click();
   await page.waitForFunction(id=>{
     const pending=JSON.parse(sessionStorage.getItem('fls-online-refresh-request')||'null');
     return pending&&pending.request_id===id;
   },failedRetryId);
   assert.equal(await failedRetryForm.locator('[name="retry_request_id"]').inputValue(),failedRetryId);
   const [retriedAfterStartFailure]=await Promise.all([
     page.waitForResponse(response=>response.url()===failedRetryEndpoint&&response.request().method()==='POST'),
     failedRetryForm.getByRole('button',{name:'明确重新同步',exact:true}).click(),
   ]);
   assert.equal(retriedAfterStartFailure.status(),202);
   assert.equal((await retriedAfterStartFailure.json()).request_id,failedRetryId);
   await page.waitForFunction(id=>!sessionStorage.getItem('fls-online-refresh-request')&&
     document.querySelector('#onlineRefreshLogLink').getAttribute('href').endsWith('/'+id),failedRetryId);
   assert.equal((await fixture({})).refreshes,1);

   const capacitySetup=await page.request.post(base+'/api/fixture/refresh-history-capacity',{headers,data:{}});
   assert.equal(capacitySetup.status(),200);
   const capacityOriginal=(await capacitySetup.json()).original;
   await page.goto(base+'/online-scripts');
   await page.waitForFunction(()=>document.querySelector('#onlineRefreshHistoryRange').textContent==='显示 1-8 / 200');
   const historyPagerBinding=await page.locator('#onlineRefreshHistoryNext').evaluate(link=>({
     onclick:typeof link.onclick,bound:link.dataset.historyBound,href:link.href,hidden:link.hidden,
     localNavigation:link.hasAttribute('data-fls-local-navigation'),
   }));
   assert.equal(historyPagerBinding.onclick,'function',JSON.stringify(historyPagerBinding));
   assert.equal(historyPagerBinding.bound,'true',JSON.stringify(historyPagerBinding));
   assert.equal(historyPagerBinding.localNavigation,true,JSON.stringify(historyPagerBinding));
   const firstHistoryPage=await page.locator('#onlineRefreshHistory').innerHTML();
   const documentNavigations=[];
   const recordHistoryDocumentNavigation=request=>{
     if(request.isNavigationRequest()&&request.url().includes('/online-scripts?refresh_history_offset='))
       documentNavigations.push(request.url());
   };
   page.on('request',recordHistoryDocumentNavigation);
   const [historyPageTwo]=await Promise.all([
     page.waitForResponse(response=>response.url().includes('/api/online-scripts/refresh-history?limit=8&offset=8&')&&response.status()===200),
     page.locator('#onlineRefreshHistoryNext').click(),
   ]);
   assert.equal(historyPageTwo.status(),200);
   await page.waitForFunction(()=>document.querySelector('#onlineRefreshHistoryRange').textContent==='显示 9-16 / 200');
   assert.equal(new URL(page.url()).searchParams.get('refresh_history_offset'),'8');
   assert.deepEqual(documentNavigations,[],'pagination should update through its history API, not a document request');
   assert.notEqual(await page.locator('#onlineRefreshHistory').innerHTML(),firstHistoryPage);
   await page.goBack();
   await page.waitForFunction(()=>document.querySelector('#onlineRefreshHistoryRange').textContent==='显示 1-8 / 200');
   assert.equal(new URL(page.url()).searchParams.get('refresh_history_offset'),'0');
   await page.goForward();
   await page.waitForFunction(()=>document.querySelector('#onlineRefreshHistoryRange').textContent==='显示 9-16 / 200');
   assert.equal(new URL(page.url()).searchParams.get('refresh_history_offset'),'8');
   const secondHistoryPage=await page.locator('#onlineRefreshHistory').innerHTML();
   const changeHistory=await page.request.post(base+'/api/fixture/refresh-history-change',{headers,data:{}});
   assert.equal(changeHistory.status(),200);
   await page.locator('#onlineRefreshHistoryNext').click();
   await page.waitForFunction(()=>document.querySelector('#onlineRefreshHistoryError').textContent.includes('HTTP 409'));
   assert.deepEqual(documentNavigations,[],'stale pagination should not navigate away while recovering');
   assert.equal(await page.locator('#onlineRefreshHistory').innerHTML(),secondHistoryPage,
     'a stale snapshot must keep the last rendered page until recovery succeeds');
   await page.locator('#onlineRefreshHistoryRetry').click();
   await page.waitForFunction(()=>document.querySelector('#onlineRefreshHistoryError').hidden&&
     document.querySelector('#onlineRefreshHistoryRange').textContent==='显示 1-8 / 200');
   assert.equal(new URL(page.url()).searchParams.get('refresh_history_offset'),'0');
   assert.equal(await page.locator('#onlineRefreshHistoryPrevious').isVisible(),false);
   assert.equal(await page.locator('#onlineRefreshHistoryNext').isVisible(),true);
   await page.screenshot({path:'/tmp/fls-refresh-history-pagination-'+viewport.width+'.png'});
   page.off('request',recordHistoryDocumentNavigation);
   if(viewport.width<1200) await page.locator('#flsFloatMenuBtn').click();
   const tasksNavigation=page.locator('.nav a[href="/tasks"]').first();
   await tasksNavigation.click();
   await page.waitForURL(url=>url.pathname==='/tasks');
   assert.equal(await page.locator('#onlineRefreshHistory').count(),0);
   await page.goBack();
   await page.waitForFunction(()=>document.querySelector('#onlineRefreshHistoryRange')?.textContent==='显示 1-8 / 200');
   assert.equal(new URL(page.url()).pathname,'/online-scripts');
   await page.goForward();
   await page.waitForURL(url=>url.pathname==='/tasks');
   assert.equal(await page.locator('#onlineRefreshHistory').count(),0);
   await page.goBack();
   await page.waitForFunction(()=>document.querySelector('#onlineRefreshHistoryRange')?.textContent==='显示 1-8 / 200');
   assert.equal(new URL(page.url()).pathname,'/online-scripts');
   if(process.env.FLS_REFRESH_HISTORY_PAGINATION_ONLY==='1'){
     assert.deepEqual(errors,[]);
     console.log('online refresh history pagination passed '+viewport.width);
     await context.close();
     continue;
   }
   await page.goto(base+'/online-scripts?refresh_request_id='+capacityOriginal+'#onlineRefreshHistory');
   const capacityForm=page.locator('#onlineRefreshHistory form[data-refresh-retry-of="'+capacityOriginal+'"]');
   await capacityForm.waitFor();
   const rejectedRetryId=await capacityForm.locator('[name="retry_request_id"]').inputValue();
   const capacityRetryEndpoint=base+'/api/online-scripts/refresh-retry/'+capacityOriginal;
   const [capacityRejected]=await Promise.all([
     page.waitForResponse(response=>response.url()===capacityRetryEndpoint&&response.request().method()==='POST'),
     capacityForm.getByRole('button',{name:'明确重新同步',exact:true}).click(),
   ]);
   assert.equal(capacityRejected.status(),409);
   assert.equal((await capacityRejected.json()).retry_not_started,true);
   await page.waitForFunction(()=>document.querySelector('#onlineRefreshHistoryError').textContent.includes('此次重试未启动'));
   assert.equal(await page.evaluate(()=>sessionStorage.getItem('fls-online-refresh-request')),null);
   assert.notEqual(await capacityForm.locator('[name="retry_request_id"]').inputValue(),rejectedRetryId);
   assert.equal(await page.locator('#onlineRefreshQuery').isHidden(),true);
   const fullSyncLink=page.getByRole('link',{name:'发起新的完整同步',exact:true});
   await fullSyncLink.scrollIntoViewIfNeeded();
   const fullSyncLinkBox=await fullSyncLink.boundingBox();
   assert.ok(fullSyncLinkBox.x>=0&&fullSyncLinkBox.x+fullSyncLinkBox.width<=viewport.width);
   assert.ok(fullSyncLinkBox.y>=0&&fullSyncLinkBox.y+fullSyncLinkBox.height<=viewport.height);
   await page.screenshot({path:'/tmp/fls-refresh-chain-capacity-'+viewport.width+'.png'});
   assert.equal((await fixture({})).refreshes,0);
   assert.equal(await fullSyncLink.getAttribute('href'),'/online-scripts#online-status');
   await fullSyncLink.click();
   await page.waitForURL(url=>url.pathname==='/online-scripts');
   const fullSyncEndpoint=base+'/api/online-scripts/refresh';
   const [fullSyncResponse]=await Promise.all([
     page.waitForResponse(response=>response.url()===fullSyncEndpoint&&response.request().method()==='POST'),
     page.locator('#onlineRefreshBtn').click(),
   ]);
   assert.equal(fullSyncResponse.status(),202);
   const fullSyncReceipt=await fullSyncResponse.json();
   assert.notEqual(fullSyncReceipt.request_id,rejectedRetryId);
   await page.waitForFunction(()=>document.body.textContent.includes('fixture retry completed'));
   assert.equal((await fixture({})).refreshes,1);

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

   const orphanSetup=await page.request.post(base+'/api/fixture/orphaned-refresh',{headers});
   assert.equal(orphanSetup.status(),200);
   const orphanId=(await orphanSetup.json()).original;
   let orphanLogPolls=0;
   const countOrphanPolls=request=>{if(request.url().includes('/api/online-scripts/refresh-log/'+orphanId)) orphanLogPolls++;};
   page.on('request',countOrphanPolls);
   await page.goto(base+'/online-scripts/refresh-log/'+orphanId);
   assert.equal(await page.locator('#refreshLogStatus').textContent(),'结果未知');
   assert.match(await page.locator('#refreshLogDiagnosis').textContent(),/original orphan diagnosis/);
   assert.match(await page.locator('#refreshLogText').textContent(),/orphaned original refresh log/);
   await page.getByRole('button',{name:'检查当前缓存结果',exact:true}).waitFor();
   await page.waitForTimeout(2300);
   assert.equal(orphanLogPolls,0);
   page.off('request',countOrphanPolls);
   await page.screenshot({path:'/tmp/fls-orphan-refresh-'+viewport.width+'.png'});
   await page.getByRole('link',{name:'从历史重新同步',exact:true}).click();
   await page.waitForURL(url=>url.searchParams.get('refresh_request_id')===orphanId);
   assert.equal(new URL(page.url()).searchParams.get('refresh_request_id'),orphanId);
   const orphanRetryForm=page.locator('#onlineRefreshHistory form[data-refresh-retry-of="'+orphanId+'"]');
   await orphanRetryForm.waitFor();
   const orphanRetryId=await orphanRetryForm.locator('[name="retry_request_id"]').inputValue();
   const orphanRetryEndpoint=base+'/api/online-scripts/refresh-retry/'+orphanId;
   await page.route(orphanRetryEndpoint,async route=>{
     const actual=await route.fetch();
     assert.equal(actual.status(),202);
     await route.abort('failed');
   });
   await orphanRetryForm.getByRole('button',{name:'明确重新同步',exact:true}).click();
   await page.locator('#onlineRefreshQuery').waitFor({state:'visible'});
   assert.equal(JSON.parse(await page.evaluate(()=>sessionStorage.getItem('fls-online-refresh-request'))).request_id,orphanRetryId);
   await page.unroute(orphanRetryEndpoint);
   await page.locator('#onlineRefreshQuery').click();
   await page.waitForFunction(()=>!sessionStorage.getItem('fls-online-refresh-request'));
   const orphanRecovered=await fixture({});
   assert.equal(orphanRecovered.refreshes,1);
   await page.locator('#onlineRefreshLogLink').click();
   await page.waitForSelector('#refreshLogText');
   assert.match(await page.locator('#refreshLogText').textContent(),/original retry completed/);
   await page.goto(base+'/online-scripts/refresh-log/'+orphanId);
   assert.equal(await page.locator('#refreshLogStatus').textContent(),'结果未知');
   assert.match(await page.locator('#refreshLogText').textContent(),/orphaned original refresh log/);

   const missingSetup=await page.request.post(base+'/api/fixture/orphaned-refresh',{headers});
   assert.equal(missingSetup.status(),200);
   const missingId=(await missingSetup.json()).original;
   const missingLogUrl=base+'/online-scripts/refresh-log/'+missingId;
   const missingStateUrl=base+'/api/online-scripts/refresh-status/'+missingId;
   const logFile=async restore=>{
     const response=await page.request.post(base+'/api/fixture/refresh-log-file',{headers,data:{request_id:missingId,restore}});
     assert.equal(response.status(),200);return response.json();
   };
   assert.equal((await logFile(false)).refreshes,0);
   const missingPage=await page.goto(missingLogUrl);
   assert.equal(missingPage.status(),404);
   assert.match(await page.locator('#refreshLogQueryResult').textContent(),/original orphan diagnosis/);
   await page.route(missingStateUrl,route=>route.fulfill({status:503,contentType:'text/html',body:'state query temporarily unavailable'}));
   await page.locator('#refreshLogQuery').click();
   await page.waitForFunction(()=>document.querySelector('#refreshLogQueryError').textContent.includes('HTTP 503'));
   assert.equal(await page.locator('#refreshLogQuery').isEnabled(),true);
   assert.match(await page.locator('#refreshLogQueryResult').textContent(),/original orphan diagnosis/);
   await page.unroute(missingStateUrl);
   await page.locator('#refreshLogQuery').click();
   await page.waitForFunction(()=>document.querySelector('#refreshLogQueryResult').textContent.startsWith('原刷新状态：结果未知'));
   await page.screenshot({path:'/tmp/fls-missing-refresh-log-'+viewport.width+'.png'});
   assert.equal((await logFile(true)).refreshes,0);
   await page.getByRole('button',{name:'重新读取日志',exact:true}).click();
   await page.locator('#refreshLogText').waitFor();
   assert.equal(new URL(page.url()).pathname,'/online-scripts/refresh-log/'+missingId);
   assert.match(await page.locator('#refreshLogText').textContent(),/restored original refresh log/);
   assert.equal((await logFile(false)).refreshes,0);
   await page.goto(missingLogUrl);
   await page.getByRole('link',{name:'返回原刷新记录',exact:true}).click();
   await page.waitForURL(url=>url.searchParams.get('refresh_request_id')===missingId);
   const missingRetry=page.locator('#onlineRefreshHistory form[data-refresh-retry-of="'+missingId+'"]');
   const [missingRetried]=await Promise.all([
     page.waitForResponse(response=>response.url()===base+'/api/online-scripts/refresh-retry/'+missingId&&response.request().method()==='POST'),
     missingRetry.getByRole('button',{name:'明确重新同步',exact:true}).click(),
   ]);
   assert.equal(missingRetried.status(),202);
   const missingRetryId=(await missingRetried.json()).request_id;
   assert.match(missingRetryId,/^[a-f0-9]{32}$/);
   await page.waitForFunction(requestId=>document.querySelector('#onlineRefreshLogLink').getAttribute('href').endsWith('/'+requestId),missingRetryId);
   await page.waitForFunction(()=>!sessionStorage.getItem('fls-online-refresh-request'));
   assert.equal((await fixture({})).refreshes,1);
   await page.locator('#onlineRefreshLogLink').click();
   await page.locator('#refreshLogText').waitFor();
   assert.match(await page.locator('#refreshLogText').textContent(),/original retry completed/);

   const delayedEndpoint=base+'/api/fixture/delayed-refresh-log';
   const delayedSeed=await page.request.post(delayedEndpoint,{headers,data:{action:'seed'}});
   assert.equal(delayedSeed.status(),200);
   const delayedId=(await delayedSeed.json()).request_id;
   let delayedLogPolls=0;
   const countDelayedPolls=request=>{if(request.url().includes('/api/online-scripts/refresh-log/'+delayedId)) delayedLogPolls++;};
   page.on('request',countDelayedPolls);
   const delayedPage=await page.goto(base+'/online-scripts/refresh-log/'+delayedId);
   assert.equal(delayedPage.status(),200);
   const waitingNotice=page.locator('#refreshLogWaiting');
   await waitingNotice.waitFor({state:'visible'});
   assert.equal(await page.locator('#refreshLogStatus').textContent(),'运行中');
   assert.equal(await page.locator('#refreshLogError').isVisible(),false);
   assert.match(await waitingNotice.textContent(),/日志尚未生成/);
   await page.screenshot({path:'/tmp/fls-delayed-refresh-log-'+viewport.width+'.png'});
   const publishDelayed=await page.request.post(delayedEndpoint,{headers,data:{action:'publish'}});
   assert.equal(publishDelayed.status(),200);
   await page.waitForFunction(()=>document.querySelector('#refreshLogText').textContent.includes('delayed refresh log available'));
   assert.equal(await waitingNotice.isHidden(),true);
   const completeDelayed=await page.request.post(delayedEndpoint,{headers,data:{action:'complete'}});
   assert.equal(completeDelayed.status(),200);
   await page.waitForFunction(()=>document.querySelector('#refreshLogStatus').textContent==='已完成');
   const terminalPollCount=delayedLogPolls;
   await page.waitForTimeout(2200);
   assert.equal(delayedLogPolls,terminalPollCount);
   page.off('request',countDelayedPolls);
   const removeDelayed=await page.request.post(delayedEndpoint,{headers,data:{action:'remove'}});
   assert.equal(removeDelayed.status(),200);
   const terminalMissing=await page.request.get(base+'/api/online-scripts/refresh-log/'+delayedId,{headers});
   assert.equal(terminalMissing.status(),404);
   assert.match((await terminalMissing.json()).error,/尚未生成或已被清理/);
   const terminalMissingPage=await page.goto(base+'/online-scripts/refresh-log/'+delayedId);
   assert.equal(terminalMissingPage.status(),404);
   assert.match(await page.locator('#refreshLogQueryResult').textContent(),/已完成/);
   assert.deepEqual(errors,[]);
   console.log('online task-link recovery passed '+viewport.width);
   await context.close();
  }
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
'''


if __name__ == "__main__":
    main()
