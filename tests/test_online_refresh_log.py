import json
import re
import shutil
import subprocess
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_ui_route_components import ROOT, TOKEN, isolated_app


class OnlineRefreshLogTests(unittest.TestCase):
    def _state(self, base_dir, *, refresh_id="a" * 32, request_id="b" * 32, log_file=None):
        from fls_manager.online_scripts.constants import update_online_refresh_state
        from fls_manager.online_scripts.source import online_script_cache_fingerprint, online_script_source_fingerprint

        path = Path(log_file or (base_dir / "log" / f"online-script-source-refresh-{refresh_id}.log"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("refresh started\nrefresh failed: upstream timeout\n", encoding="utf-8")
        update_online_refresh_state(
            id=refresh_id,
            request_id=request_id,
            proxy_id="",
            running=False,
            status="失败",
            message="",
            error="upstream timeout",
            cache_published=False,
            task_link_failures=[],
            updated_at="now",
            log_file=str(path),
            expected_cache_fingerprint=online_script_cache_fingerprint(),
            source_fingerprint=online_script_source_fingerprint(),
        )
        return request_id

    def _orphaned_refresh(self, base_dir, *, active_current=False):
        from fls_manager.online_scripts import constants

        original = self._state(base_dir)
        constants.update_online_refresh_state(running=True, status="运行中", error="original diagnosis")
        self._state(base_dir, refresh_id="c" * 32, request_id="d" * 32)
        if active_current:
            constants.update_online_refresh_state(running=True, status="运行中")
        else:
            constants.ONLINE_REFRESH_STATE.clear()
        return original

    def test_orphaned_running_history_recovers_from_each_query_or_recovery_entry(self):
        for entry in ("status", "history", "log", "page", "retry", "confirm"):
            with self.subTest(entry=entry), isolated_app() as (app, base_dir):
                from fls_manager.online_scripts import constants
                from fls_manager.routes.online_scripts import refresh

                original = self._orphaned_refresh(base_dir)
                current_before = constants.ONLINE_REFRESH_STATE_FILE.read_bytes()
                client = app.test_client()
                headers = {"X-Token": TOKEN}
                endpoint = {
                    "status": f"/api/online-scripts/refresh-status/{original}",
                    "history": "/api/online-scripts/refresh-history",
                    "log": f"/api/online-scripts/refresh-log/{original}",
                    "page": f"/online-scripts/refresh-log/{original}",
                    "retry": f"/api/online-scripts/refresh-retry/{original}",
                    "confirm": f"/api/online-scripts/refresh-confirm/{original}",
                }[entry]
                with patch.object(refresh, "start_refresh_thread") as start:
                    if entry in ("retry", "confirm"):
                        response = client.post(endpoint, json={"retry_request_id": "e" * 32}, headers=headers)
                    else:
                        response = client.get(endpoint, headers=headers)
                self.assertEqual(response.status_code, 202 if entry == "retry" else 200)
                saved = json.loads(constants.ONLINE_REFRESH_HISTORY_FILE.read_text())[original]
                self.assertFalse(saved["running"])
                self.assertEqual(saved["status"], "已确认未发布" if entry == "confirm" else "结果未知")
                self.assertEqual(saved["id"], "a" * 32)
                self.assertIn("original diagnosis", saved["error"])
                self.assertTrue(saved["log_file"].endswith("a" * 32 + ".log"))
                if entry == "retry":
                    self.assertEqual(response.json["retry_of"], original)
                    start.assert_called_once()
                else:
                    start.assert_not_called()
                    self.assertEqual(constants.ONLINE_REFRESH_STATE_FILE.read_bytes(), current_before)
                if entry == "page":
                    html = response.get_data(as_text=True)
                    self.assertIn("结果未知", html)
                    self.assertIn("检查当前缓存结果", html)
                    self.assertIn("从历史重新同步", html)
                    self.assertIn("original diagnosis", html)
                if entry == "log":
                    self.assertFalse(response.json["running"])
                    self.assertTrue(response.json["terminal"])
                    self.assertIn("refresh failed", response.json["log"])

    def test_orphan_recovery_keeps_current_worker_running_and_is_idempotent(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import constants

            original = self._orphaned_refresh(base_dir, active_current=True)
            current_before = constants.ONLINE_REFRESH_STATE_FILE.read_bytes()
            client = app.test_client()
            response = client.get("/api/online-scripts/refresh-history", headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 200)
            items = {value["request_id"]: value for value in response.json["items"]}
            self.assertFalse(items[original]["running"])
            self.assertEqual(items[original]["status"], "结果未知")
            self.assertTrue(items["d" * 32]["running"])
            history_after = constants.ONLINE_REFRESH_HISTORY_FILE.read_bytes()
            repeated = client.get(f"/api/online-scripts/refresh-status/{original}", headers={"X-Token": TOKEN})
            self.assertEqual(repeated.status_code, 200)
            self.assertEqual(constants.ONLINE_REFRESH_HISTORY_FILE.read_bytes(), history_after)
            self.assertEqual(constants.ONLINE_REFRESH_STATE_FILE.read_bytes(), current_before)
            self.assertTrue(constants.ONLINE_REFRESH_STATE["running"])

    def test_orphan_recovery_publication_failures_follow_actual_history_without_losing_current(self):
        for after_publication in (False, True):
            with self.subTest(after_publication=after_publication), isolated_app() as (app, base_dir):
                from fls_manager.online_scripts import constants

                original = self._orphaned_refresh(base_dir)
                history_before = constants.ONLINE_REFRESH_HISTORY_FILE.read_bytes()
                current_before = constants.ONLINE_REFRESH_STATE_FILE.read_bytes()
                real_replace = constants.os.replace

                def fail_history(src, dst):
                    if Path(dst) == constants.ONLINE_REFRESH_HISTORY_FILE:
                        if after_publication:
                            real_replace(src, dst)
                        raise OSError("historical recovery publication denied")
                    return real_replace(src, dst)

                client = app.test_client()
                with patch.object(constants.os, "replace", side_effect=fail_history):
                    failed = client.get(f"/api/online-scripts/refresh-status/{original}", headers={"X-Token": TOKEN})
                self.assertEqual(failed.status_code, 500)
                self.assertIn("historical recovery publication denied", failed.json["error"])
                if not after_publication:
                    self.assertEqual(constants.ONLINE_REFRESH_HISTORY_FILE.read_bytes(), history_before)
                recovered = client.get(f"/api/online-scripts/refresh-status/{original}", headers={"X-Token": TOKEN})
                self.assertEqual(recovered.status_code, 200)
                self.assertFalse(recovered.json["running"])
                self.assertEqual(recovered.json["status"], "结果未知")
                self.assertIn("original diagnosis", recovered.json["error"])
                self.assertEqual(constants.ONLINE_REFRESH_STATE_FILE.read_bytes(), current_before)
                self.assertEqual(list(constants.ONLINE_REFRESH_HISTORY_FILE.parent.glob(".*.tmp")), [])

    def test_orphan_history_query_and_retry_claim_do_not_overwrite_each_other(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import constants
            from fls_manager.routes.online_scripts import refresh

            original = self._orphaned_refresh(base_dir)
            gate = threading.Barrier(3)
            replies, errors = {}, []

            def query(kind):
                try:
                    client = app.test_client()
                    gate.wait(timeout=5)
                    if kind == "retry":
                        response = client.post(
                            f"/api/online-scripts/refresh-retry/{original}",
                            json={"retry_request_id": "e" * 32}, headers={"X-Token": TOKEN},
                        )
                    else:
                        path = "/api/online-scripts/refresh-history" if kind == "history" else f"/api/online-scripts/refresh-status/{original}"
                        response = client.get(path, headers={"X-Token": TOKEN})
                    replies[kind] = response.status_code
                except Exception as exc:
                    errors.append(exc)

            with patch.object(refresh, "start_refresh_thread") as start:
                threads = [threading.Thread(target=query, args=(kind,), daemon=True) for kind in ("status", "history", "retry")]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)
                self.assertFalse(any(thread.is_alive() for thread in threads))
                self.assertEqual(errors, [])
                self.assertEqual(replies, {"status": 200, "history": 200, "retry": 202})
                replay = app.test_client().post(
                    f"/api/online-scripts/refresh-retry/{original}", json={"retry_request_id": "e" * 32}, headers={"X-Token": TOKEN},
                )
                self.assertEqual(replay.status_code, 200)
                self.assertTrue(replay.json["replayed"])
                start.assert_called_once()
            history = json.loads(constants.ONLINE_REFRESH_HISTORY_FILE.read_text())
            self.assertEqual(history[original]["status"], "结果未知")
            self.assertFalse(history[original]["running"])
            self.assertTrue(history["e" * 32]["running"])
            self.assertEqual(history["e" * 32]["retry_of"], original)

    def test_orphan_recovery_invalid_selection_auth_and_csrf_leave_history_unchanged(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import constants
            from fls_manager.routes.online_scripts import refresh
            from fls_manager.models import save_proxies

            save_proxies([{"id": "wrong", "name": "Wrong binding", "type": "http", "host": "127.0.0.1", "port": "8080", "enabled": True}])
            original = self._orphaned_refresh(base_dir)
            before = constants.ONLINE_REFRESH_HISTORY_FILE.read_bytes()
            current_before = constants.ONLINE_REFRESH_STATE_FILE.read_bytes()
            client = app.test_client()
            with client.session_transaction() as session:
                session.update(token=TOKEN, token_expire_at=4102444800, security_verified=True, csrf_token="orphan-csrf")
            anonymous = app.test_client()
            with anonymous.session_transaction() as session:
                session["csrf_token"] = "anonymous-csrf"
            retry_path = f"/api/online-scripts/refresh-retry/{original}"
            with patch.object(refresh, "start_refresh_thread") as start:
                denied = anonymous.post(retry_path, json={"retry_request_id": "e" * 32}, headers={"X-CSRF-Token": "anonymous-csrf"})
                self.assertEqual(denied.status_code, 401)
                csrf_denied = client.post(retry_path, json={"retry_request_id": "e" * 32})
                self.assertEqual(csrf_denied.status_code, 400)
                bound = client.post(retry_path, json={"retry_request_id": "e" * 32, "proxy_id": "wrong"}, headers={"X-Token": TOKEN})
                self.assertEqual(bound.status_code, 409)
                missing_proxy = client.post(retry_path, json={"retry_request_id": "e" * 32, "proxy_id": "missing"}, headers={"X-Token": TOKEN})
                self.assertEqual(missing_proxy.status_code, 400)
                invalid = client.post(retry_path, json={"retry_request_id": "invalid"}, headers={"X-Token": TOKEN})
                self.assertEqual(invalid.status_code, 400)
                with patch.object(refresh, "online_script_cache_fingerprint", return_value="f" * 64):
                    stale = client.post(retry_path, json={"retry_request_id": "e" * 32}, headers={"X-Token": TOKEN})
                    confirm = client.post(f"/api/online-scripts/refresh-confirm/{original}", headers={"X-Token": TOKEN})
                self.assertEqual(stale.status_code, 409)
                self.assertEqual(confirm.status_code, 409)
                start.assert_not_called()
            self.assertEqual(constants.ONLINE_REFRESH_HISTORY_FILE.read_bytes(), before)
            self.assertEqual(constants.ONLINE_REFRESH_STATE_FILE.read_bytes(), current_before)

    def test_missing_selected_history_does_not_recover_unrelated_current_request(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import constants

            self._state(base_dir)
            constants.update_online_refresh_state(running=True, status="运行中")
            constants.ONLINE_REFRESH_STATE.clear()
            state_before = constants.ONLINE_REFRESH_STATE_FILE.read_bytes()
            history_before = constants.ONLINE_REFRESH_HISTORY_FILE.read_bytes()
            client = app.test_client()
            headers = {"X-Token": TOKEN}
            for query, expected in (("invalid", 400), ("f" * 32, 404)):
                for endpoint, key in (("/online-scripts", "refresh_request_id"), ("/api/online-scripts/refresh-history", "request_id")):
                    response = client.get(f"{endpoint}?{key}={query}", headers=headers)
                    self.assertEqual(response.status_code, expected)
            self.assertEqual(constants.ONLINE_REFRESH_STATE_FILE.read_bytes(), state_before)
            self.assertEqual(constants.ONLINE_REFRESH_HISTORY_FILE.read_bytes(), history_before)
            self.assertEqual(constants.ONLINE_REFRESH_STATE, {})

    def test_orphaned_retry_child_replays_as_unknown_without_launching_again(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import constants
            from fls_manager.routes.online_scripts import refresh

            original = self._state(base_dir)
            child = "e" * 32
            constants.update_online_refresh_state(
                request_id=child, id="f" * 32, retry_of=original, running=True, status="运行中",
            )
            self._state(base_dir, request_id="d" * 32, refresh_id="c" * 32)
            constants.ONLINE_REFRESH_STATE.clear()
            with patch.object(refresh, "start_refresh_thread") as start:
                replay = app.test_client().post(
                    f"/api/online-scripts/refresh-retry/{original}", json={"retry_request_id": child}, headers={"X-Token": TOKEN},
                )
            self.assertEqual(replay.status_code, 200)
            self.assertTrue(replay.json["replayed"])
            self.assertEqual(replay.json["request_id"], child)
            self.assertEqual(replay.json["retry_of"], original)
            self.assertEqual(replay.json["status"], "结果未知")
            self.assertFalse(replay.json["running"])
            self.assertFalse(json.loads(constants.ONLINE_REFRESH_HISTORY_FILE.read_text())[child]["running"])
            start.assert_not_called()

    def test_orphaned_form_recovery_preserves_csrf_and_can_retry_failed_history_publication(self):
        for action in ("retry", "confirm"):
            for after_publication in (False, True):
                with self.subTest(action=action, after_publication=after_publication), isolated_app() as (app, base_dir):
                    from fls_manager.online_scripts import constants
                    from fls_manager.routes.online_scripts import refresh

                    original = self._orphaned_refresh(base_dir)
                    client = app.test_client()
                    with client.session_transaction() as session:
                        session.update(token=TOKEN, token_expire_at=4102444800, security_verified=True, csrf_token="orphan-form-csrf")
                    endpoint = f"/online-scripts/refresh-{action}/{original}"
                    values = {"retry_request_id": "e" * 32, "proxy_id": ""}
                    before = constants.ONLINE_REFRESH_HISTORY_FILE.read_bytes()
                    denied = client.post(endpoint, data=values)
                    self.assertEqual(denied.status_code, 400)
                    self.assertEqual(constants.ONLINE_REFRESH_HISTORY_FILE.read_bytes(), before)
                    real_replace = constants.os.replace

                    def fail_history(src, dst):
                        if Path(dst) == constants.ONLINE_REFRESH_HISTORY_FILE:
                            if after_publication:
                                real_replace(src, dst)
                            raise OSError("form recovery publication denied")
                        return real_replace(src, dst)

                    values["csrf_token"] = "orphan-form-csrf"
                    with patch.object(refresh, "start_refresh_thread") as start:
                        with patch.object(constants.os, "replace", side_effect=fail_history):
                            failed = client.post(endpoint, data=values)
                        self.assertEqual(failed.status_code, 500)
                        self.assertIn("form recovery publication denied", failed.get_data(as_text=True))
                        self.assertIn(f"/online-scripts?refresh_request_id={original}#onlineRefreshHistory", failed.get_data(as_text=True))
                        start.assert_not_called()
                        recovered = client.post(endpoint, data=values)
                        self.assertEqual(recovered.status_code, 302)
                        replay = client.post(endpoint, data=values)
                        self.assertEqual(replay.status_code, 200 if action == "retry" else 302)
                        self.assertEqual(start.call_count, 1 if action == "retry" else 0)
                    saved = json.loads(constants.ONLINE_REFRESH_HISTORY_FILE.read_text())[original]
                    self.assertFalse(saved["running"])
                    self.assertIn("original diagnosis", saved["error"])

    def test_log_recovery_keeps_selected_receipt_outside_recent_history_window(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import constants

            original = self._orphaned_refresh(base_dir)
            for index in range(10):
                constants.update_online_refresh_state(
                    request_id=f"{index + 16:032x}", id=f"{index + 32:032x}",
                    running=False, status="已完成", updated_at=f"2099-01-01 00:00:{index:02d}",
                )
            client = app.test_client()
            headers = {"X-Token": TOKEN}
            log_page = client.get(f"/online-scripts/refresh-log/{original}", headers=headers)
            self.assertEqual(log_page.status_code, 200)
            recovery_url = f"/online-scripts?refresh_request_id={original}#onlineRefreshHistory"
            self.assertIn(f'href="{recovery_url}"', log_page.get_data(as_text=True))
            recent = client.get("/api/online-scripts/refresh-history?limit=8", headers=headers)
            self.assertNotIn(original, {item["request_id"] for item in recent.json["items"]})
            selected = client.get(f"/api/online-scripts/refresh-history?limit=8&request_id={original}", headers=headers)
            self.assertEqual(selected.status_code, 200)
            self.assertEqual(len(selected.json["items"]), 8)
            self.assertEqual(selected.json["items"][0]["request_id"], original)
            page = client.get(recovery_url, headers=headers)
            self.assertEqual(page.status_code, 200)
            html = page.get_data(as_text=True)
            self.assertIn(f'action="/online-scripts/refresh-retry/{original}"', html)
            self.assertIn(f'refresh-history?limit=8&request_id={original}', html)
            self.assertIn('name="csrf_token"', html)
            before = constants.ONLINE_REFRESH_HISTORY_FILE.read_bytes()
            current_before = constants.ONLINE_REFRESH_STATE_FILE.read_bytes()
            for query, status in (("bad", 400), ("f" * 32, 404)):
                invalid = client.get(f"/api/online-scripts/refresh-history?request_id={query}", headers=headers)
                self.assertEqual(invalid.status_code, status)
                self.assertEqual(invalid.json["items"], [])
                invalid_page = client.get(f"/online-scripts?refresh_request_id={query}", headers=headers)
                self.assertEqual(invalid_page.status_code, status)
                self.assertIn("重新选择", invalid_page.get_data(as_text=True))
            self.assertEqual(constants.ONLINE_REFRESH_HISTORY_FILE.read_bytes(), before)
            self.assertEqual(constants.ONLINE_REFRESH_STATE_FILE.read_bytes(), current_before)

    def test_missing_refresh_log_preserves_receipt_and_executable_recovery_urls(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import constants

            request_id = self._state(base_dir)
            constants.update_online_refresh_state(status="结果未知", error="original unknown result")
            path = Path(constants.ONLINE_REFRESH_STATE["log_file"])
            path.unlink()
            state_before = constants.ONLINE_REFRESH_STATE_FILE.read_bytes()
            history_before = constants.ONLINE_REFRESH_HISTORY_FILE.read_bytes()
            client = app.test_client()
            headers = {"X-Token": TOKEN}
            log_url = f"/online-scripts/refresh-log/{request_id}"
            state_url = f"/api/online-scripts/refresh-status/{request_id}"
            history_url = f"/online-scripts?refresh_request_id={request_id}#onlineRefreshHistory"
            failed = client.get("/api" + log_url, headers=headers)
            self.assertEqual(failed.status_code, 404)
            self.assertEqual(failed.json["state_url"], state_url)
            self.assertEqual(failed.json["log_url"], log_url)
            self.assertEqual(failed.json["history_url"], history_url)
            self.assertEqual(failed.json["refresh_state"]["status"], "结果未知")
            self.assertEqual(failed.json["refresh_state"]["error"], "original unknown result")
            page = client.get(log_url, headers=headers)
            self.assertEqual(page.status_code, 404)
            html = page.get_data(as_text=True)
            self.assertIn(f'href="{log_url}"', html)
            self.assertNotIn(f'href="/api{log_url}"', html)
            self.assertIn(f'href="{history_url}"', html)
            self.assertIn('id="refreshLogQuery"', html)
            self.assertIn('onclick="flsRefreshCurrentPage()"', html)
            self.assertIn("original unknown result", html)
            self.assertEqual(constants.ONLINE_REFRESH_STATE_FILE.read_bytes(), state_before)
            self.assertEqual(constants.ONLINE_REFRESH_HISTORY_FILE.read_bytes(), history_before)
            path.write_text("recovered original log\n", encoding="utf-8")
            restored = client.get(log_url, headers=headers)
            self.assertEqual(restored.status_code, 200)
            self.assertIn("recovered original log", restored.get_data(as_text=True))

    def test_missing_log_query_auth_and_invalid_identity_do_not_mutate_receipts(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import constants

            request_id = self._state(base_dir)
            Path(constants.ONLINE_REFRESH_STATE["log_file"]).unlink()
            state_before = constants.ONLINE_REFRESH_STATE_FILE.read_bytes()
            history_before = constants.ONLINE_REFRESH_HISTORY_FILE.read_bytes()
            client = app.test_client()
            for endpoint in (f"/api/online-scripts/refresh-log/{request_id}", f"/api/online-scripts/refresh-status/{request_id}"):
                self.assertEqual(client.get(endpoint).status_code, 401)
            bad = client.get("/api/online-scripts/refresh-log/invalid-id", headers={"X-Token": TOKEN})
            self.assertEqual(bad.status_code, 400)
            self.assertEqual(bad.json["state_url"], "")
            self.assertEqual(bad.json["retry_url"], "")
            self.assertIsNone(bad.json["refresh_state"])
            html = client.get("/online-scripts/refresh-log/invalid-id", headers={"X-Token": TOKEN})
            self.assertEqual(html.status_code, 400)
            self.assertNotIn('id="refreshLogQuery"', html.get_data(as_text=True))
            self.assertEqual(constants.ONLINE_REFRESH_STATE_FILE.read_bytes(), state_before)
            self.assertEqual(constants.ONLINE_REFRESH_HISTORY_FILE.read_bytes(), history_before)

    def test_unreadable_refresh_logs_keep_original_state_and_do_not_read_unsafe_paths(self):
        for failure in ("symlink", "directory", "tail-error", "tail-diagnostic"):
            with self.subTest(failure=failure), isolated_app() as (app, base_dir):
                from fls_manager.online_scripts import constants
                from fls_manager.routes.online_scripts import logs, refresh

                request_id = self._state(base_dir)
                constants.update_online_refresh_state(status="结果未知", error="original diagnosis")
                path = Path(constants.ONLINE_REFRESH_STATE["log_file"])
                outside = base_dir / "private-log.txt"
                outside.write_text("private content", encoding="utf-8")
                if failure in ("symlink", "directory"):
                    path.unlink()
                    if failure == "symlink":
                        path.symlink_to(outside)
                    else:
                        path.mkdir()
                state_before = constants.ONLINE_REFRESH_STATE_FILE.read_bytes()
                history_before = constants.ONLINE_REFRESH_HISTORY_FILE.read_bytes()
                with patch.object(logs, "tail_file") as tail, patch.object(refresh, "start_refresh_thread") as start:
                    if failure == "tail-error":
                        tail.side_effect = PermissionError("refresh log access denied")
                    else:
                        tail.return_value = "读取日志失败: refresh log access denied"
                    client = app.test_client()
                    headers = {"X-Token": TOKEN}
                    result = client.get(f"/api/online-scripts/refresh-log/{request_id}", headers=headers)
                    page = client.get(f"/online-scripts/refresh-log/{request_id}", headers=headers)
                    self.assertEqual(result.status_code, 500)
                    self.assertEqual(page.status_code, 500)
                    self.assertEqual(result.json["refresh_state"]["status"], "结果未知")
                    self.assertEqual(result.json["refresh_state"]["error"], "original diagnosis")
                    self.assertIn('id="refreshLogQuery"', page.get_data(as_text=True))
                    self.assertNotIn("private content", page.get_data(as_text=True))
                    if failure in ("symlink", "directory"):
                        tail.assert_not_called()
                    start.assert_not_called()
                self.assertEqual(constants.ONLINE_REFRESH_STATE_FILE.read_bytes(), state_before)
                self.assertEqual(constants.ONLINE_REFRESH_HISTORY_FILE.read_bytes(), history_before)
                self.assertEqual(outside.read_text(encoding="utf-8"), "private content")

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise refresh receipt queries")
    def test_missing_refresh_log_query_preserves_result_on_errors_and_merges_concurrent_clicks(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import constants

            request_id = self._state(base_dir)
            Path(constants.ONLINE_REFRESH_STATE["log_file"]).unlink()
            html = app.test_client().get(f"/online-scripts/refresh-log/{request_id}", headers={"X-Token": TOKEN}).get_data(as_text=True)
            script = next(script for script in re.findall(r"<script>(.*?)</script>", html, re.S) if "loadRefreshReceipt" in script)
            static = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
            reader = static[static.index("async function flsReadJsonResponse("):static.index("async function flsReadTextResponse(")]
            result = subprocess.run(["node", "-e", r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
(async()=>{
 const elements={refreshLogQuery:{disabled:false},refreshLogQueryResult:{textContent:'original diagnosis'},refreshLogQueryError:{hidden:true,textContent:''},refreshLogQueryLogin:{hidden:true,href:''}};
 let click,calls=0,finish;
 elements.refreshLogQuery.addEventListener=(_name,fn)=>{click=fn;};
 const context={document:{getElementById:id=>elements[id]},location:{pathname:'/online-scripts/refresh-log/'+input.request_id,search:''},encodeURIComponent,
   flsResponseErrorDiagnostic:(_r,body)=>{try{return {payload:JSON.parse(body),message:'backend diagnostic'};}catch{return {payload:null,message:body};}}};
 vm.createContext(context);vm.runInContext(input.reader+input.script,context);
 for(const status of [401,403,404,409,500,503,200]){
   context.fetch=async(url,options)=>{
     assert.equal(url,'/api/online-scripts/refresh-status/'+input.request_id);
     assert.equal(options.method,undefined);
     return {ok:status===200,status,text:async()=>'<html>temporarily unavailable</html>'};
   };
   await click();
   assert.equal(elements.refreshLogQuery.disabled,false);
   assert.match(elements.refreshLogQueryError.textContent,/查询失败/);
   assert.equal(elements.refreshLogQueryResult.textContent,'original diagnosis');
   assert.equal(elements.refreshLogQueryLogin.hidden,status!==401&&status!==403);
   if(status===401||status===403) assert.match(elements.refreshLogQueryLogin.href,/next=%2Fonline-scripts%2Frefresh-log%2F/);
 }
 context.fetch=async()=>({ok:true,status:200,json:async()=>({request_id:'f'.repeat(32),status:'已完成',running:false})});
 await click();
 assert.equal(elements.refreshLogQueryResult.textContent,'original diagnosis');
 context.fetch=async()=>{calls++;return new Promise(resolve=>{finish=resolve;});};
 const first=click();await click();assert.equal(calls,1);assert.equal(elements.refreshLogQuery.disabled,true);
 finish({ok:true,status:200,json:async()=>({request_id:input.request_id,status:'结果未知',running:false,error:'original recovered diagnosis'})});
 await first;
 assert.equal(elements.refreshLogQuery.disabled,false);assert.equal(elements.refreshLogQueryError.hidden,true);
 assert.equal(elements.refreshLogQueryResult.textContent,'原刷新状态：结果未知；original recovered diagnosis');
 console.log('missing refresh log receipt query passed');
})().catch(e=>{console.error(e);process.exitCode=1});
'''], input=json.dumps({"script": script, "reader": reader, "request_id": request_id}), text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_refresh_log_api_and_page_bind_request_and_bound_file(self):
        with isolated_app() as (app, base_dir):
            request_id = self._state(base_dir)
            client = app.test_client()
            response = client.get(
                f"/api/online-scripts/refresh-log/{request_id}?lines=20",
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json["ok"])
            self.assertEqual(response.json["request_id"], request_id)
            self.assertTrue(response.json["terminal"])
            self.assertIn("refresh failed", response.json["log"])
            page = client.get(f"/online-scripts/refresh-log/{request_id}", headers={"X-Token": TOKEN})
            self.assertEqual(page.status_code, 200)
            self.assertIn("refresh failed", page.get_data(as_text=True))
            self.assertIn(f"/api/online-scripts/refresh-log/{request_id}", page.get_data(as_text=True))
            status_page = client.get("/online-scripts", headers={"X-Token": TOKEN})
            self.assertEqual(status_page.status_code, 200)
            status_html = status_page.get_data(as_text=True)
            self.assertIn(f'id="onlineRefreshLogLink"', status_html)
            self.assertIn(
                f'id="onlineRefreshLogLink" class="btn btn-blue" href="/online-scripts/refresh-log/{request_id}"',
                status_html,
            )
            self.assertNotIn(
                f'id="onlineRefreshLogLink" class="btn btn-blue" href="/online-scripts/refresh-log/{request_id}" hidden',
                status_html,
            )
            invalid_lines = client.get(
                f"/api/online-scripts/refresh-log/{request_id}?lines=bad",
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(invalid_lines.status_code, 400)

    def test_refresh_log_rejects_mismatch_symlink_and_outside_path_without_reading(self):
        with isolated_app() as (app, base_dir):
            request_id = self._state(base_dir)
            client = app.test_client()
            missing = client.get(
                "/api/online-scripts/refresh-log/" + "c" * 32,
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(missing.status_code, 404)

            from fls_manager.online_scripts.constants import update_online_refresh_state

            outside = base_dir / "outside.log"
            outside.write_text("outside", encoding="utf-8")
            update_online_refresh_state(log_file=str(outside))
            rejected = client.get(
                f"/api/online-scripts/refresh-log/{request_id}",
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(rejected.status_code, 500)
            self.assertIn("受控日志目录", rejected.json["error"])

            expected = base_dir / "log" / f"online-script-source-refresh-{'a' * 32}.log"
            link = base_dir / "log" / "online-script-source-refresh-link.log"
            try:
                link.symlink_to(expected)
            except (OSError, NotImplementedError):
                self.skipTest("symlink is unavailable")
            update_online_refresh_state(log_file=str(link))
            symlink_response = client.get(
                f"/api/online-scripts/refresh-log/{request_id}",
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(symlink_response.status_code, 500)
            self.assertIn("日志路径", symlink_response.json["error"])

    def test_refresh_log_state_marks_running_until_terminal(self):
        with isolated_app() as (app, base_dir):
            request_id = self._state(base_dir)
            from fls_manager.online_scripts.constants import update_online_refresh_state

            update_online_refresh_state(running=True, status="运行中", message="still working")
            client = app.test_client()
            running = client.get(
                f"/api/online-scripts/refresh-log/{request_id}", headers={"X-Token": TOKEN},
            )
            self.assertEqual(running.status_code, 200)
            self.assertFalse(running.json["terminal"])
            self.assertTrue(running.json["running"])

            update_online_refresh_state(running=False, status="已完成", message="done")
            terminal = client.get(
                f"/api/online-scripts/refresh-log/{request_id}", headers={"X-Token": TOKEN},
            )
            self.assertEqual(terminal.status_code, 200)
            self.assertTrue(terminal.json["terminal"])
            self.assertFalse(terminal.json["running"])

    def test_refresh_log_waits_for_missing_log_only_while_job_is_running(self):
        with isolated_app() as (app, base_dir):
            request_id = self._state(base_dir)
            from fls_manager.online_scripts.constants import update_online_refresh_state

            log_file = base_dir / "log" / f"online-script-source-refresh-{'a' * 32}.log"
            log_file.unlink()
            update_online_refresh_state(
                running=True, status="运行中", message="still working", error="", log_file=str(log_file),
            )
            client = app.test_client()
            headers = {"X-Token": TOKEN}

            waiting = client.get(f"/api/online-scripts/refresh-log/{request_id}", headers=headers)
            self.assertEqual(waiting.status_code, 200)
            self.assertTrue(waiting.json["ok"])
            self.assertTrue(waiting.json["running"])
            self.assertFalse(waiting.json["terminal"])
            self.assertTrue(waiting.json["log_pending"])
            self.assertEqual(waiting.json["log_status"], 404)
            self.assertEqual(waiting.json["log"], "暂无日志")

            page = client.get(f"/online-scripts/refresh-log/{request_id}", headers=headers)
            html = page.get_data(as_text=True)
            self.assertEqual(page.status_code, 200)
            self.assertIn('id="refreshLogWaiting" role="status">刷新仍在运行', html)
            self.assertIn("loadRefreshLog();", html)

            log_file.write_text("refresh log appeared\n", encoding="utf-8")
            available = client.get(f"/api/online-scripts/refresh-log/{request_id}", headers=headers)
            self.assertEqual(available.status_code, 200)
            self.assertFalse(available.json["log_pending"])
            self.assertEqual(available.json["log_status"], 200)
            self.assertIn("refresh log appeared", available.json["log"])

            update_online_refresh_state(running=False, status="已完成", error="")
            completed = client.get(f"/api/online-scripts/refresh-log/{request_id}", headers=headers)
            self.assertEqual(completed.status_code, 200)
            self.assertTrue(completed.json["terminal"])
            self.assertFalse(completed.json["running"])

            log_file.unlink()
            terminal_missing = client.get(f"/api/online-scripts/refresh-log/{request_id}", headers=headers)
            self.assertEqual(terminal_missing.status_code, 404)
            self.assertIn("尚未生成或已被清理", terminal_missing.json["error"])

    def test_refresh_log_keeps_old_request_after_new_refresh_replaces_current(self):
        with isolated_app() as (app, base_dir):
            old_request = self._state(base_dir, refresh_id="a" * 32, request_id="b" * 32)
            from fls_manager.online_scripts import constants
            from fls_manager.online_scripts.constants import update_online_refresh_state

            (base_dir / "data" / "online_script_refresh_requests.json").unlink()
            update_online_refresh_state(status="部分完成", message="old partial refresh")
            new_log = base_dir / "log" / f"online-script-source-refresh-{'c' * 32}.log"
            new_log.write_text("new refresh log\n", encoding="utf-8")
            update_online_refresh_state(
                id="c" * 32,
                request_id="d" * 32,
                running=False,
                status="已完成",
                message="new refresh complete",
                error="",
                cache_published=True,
                task_link_failures=[],
                updated_at="later",
                log_file=str(new_log),
            )
            client = app.test_client()
            old_log = client.get(
                f"/api/online-scripts/refresh-log/{old_request}", headers={"X-Token": TOKEN},
            )
            self.assertEqual(old_log.status_code, 200)
            self.assertEqual(old_log.json["id"], "a" * 32)
            self.assertIn("refresh failed", old_log.json["log"])
            old_status = client.get(
                f"/api/online-scripts/refresh-status/{old_request}", headers={"X-Token": TOKEN},
            )
            self.assertEqual(old_status.status_code, 200)
            self.assertEqual(old_status.json["id"], "a" * 32)
            replayed = client.post(
                "/api/online-scripts/refresh",
                json={"proxy_id": "", "request_id": old_request},
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(replayed.status_code, 200)
            self.assertTrue(replayed.json["replayed"])
            self.assertEqual(replayed.json["id"], "a" * 32)
            current = client.get(
                "/api/online-scripts/refresh-status/" + "d" * 32, headers={"X-Token": TOKEN},
            )
            self.assertEqual(current.status_code, 200)
            self.assertEqual(current.json["id"], "c" * 32)
            history = client.get(
                "/api/online-scripts/refresh-history?limit=10", headers={"X-Token": TOKEN},
            )
            self.assertEqual(history.status_code, 200)
            self.assertTrue(history.json["ok"])
            history_ids = {item["request_id"] for item in history.json["items"]}
            self.assertIn(old_request, history_ids)
            self.assertIn("d" * 32, history_ids)
            status_page = client.get("/online-scripts", headers={"X-Token": TOKEN})
            self.assertEqual(status_page.status_code, 200)
            status_html = status_page.get_data(as_text=True)
            self.assertIn('id="onlineRefreshHistory"', status_html)
            self.assertIn(f"/online-scripts/refresh-log/{old_request}", status_html)
            history_before_errors = constants.ONLINE_REFRESH_HISTORY_FILE.read_bytes()
            anonymous = app.test_client()
            self.assertEqual(
                anonymous.get("/api/online-scripts/refresh-history").status_code,
                401,
            )
            with patch("fls_manager.auth.query_token_enabled", return_value=False):
                forbidden = client.get("/api/online-scripts/refresh-history?token=invalid")
            self.assertEqual(forbidden.status_code, 403)
            invalid_limit = client.get(
                "/api/online-scripts/refresh-history?limit=bad", headers={"X-Token": TOKEN},
            )
            self.assertEqual(invalid_limit.status_code, 400)
            missing = client.get(
                "/api/online-scripts/refresh-history?request_id=" + "f" * 32,
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(missing.status_code, 404)
            self.assertEqual(missing.json["items"], [])
            self.assertEqual(constants.ONLINE_REFRESH_HISTORY_FILE.read_bytes(), history_before_errors)
            with patch(
                "fls_manager.routes.online_scripts.refresh.get_online_refresh_history",
                side_effect=RuntimeError("history disk unavailable"),
            ):
                failed_api = client.get(
                    "/api/online-scripts/refresh-history", headers={"X-Token": TOKEN},
                )
            self.assertEqual(failed_api.status_code, 500)
            self.assertEqual(failed_api.json["items"], [])
            self.assertEqual(constants.ONLINE_REFRESH_HISTORY_FILE.read_bytes(), history_before_errors)
            with patch(
                "fls_manager.routes.online_scripts.pages.get_online_refresh_history",
                side_effect=RuntimeError("history disk unavailable"),
            ):
                failed_page = client.get("/online-scripts", headers={"X-Token": TOKEN})
            self.assertEqual(failed_page.status_code, 200)
            self.assertIn("历史无法读取", failed_page.get_data(as_text=True))

    def test_corrupt_refresh_history_is_reported_without_falling_back_to_new_log(self):
        with isolated_app() as (app, base_dir):
            self._state(base_dir)
            history = base_dir / "data" / "online_script_refresh_requests.json"
            history.write_text("{not-json", encoding="utf-8")
            client = app.test_client()
            response = client.get(
                "/api/online-scripts/refresh-log/" + "c" * 32,
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(response.status_code, 500)
            self.assertIn("历史无法读取", response.json["error"])
            self.assertNotIn("refresh failed", response.json["log"])

    def test_failed_refresh_history_has_explicit_idempotent_retry(self):
        with isolated_app() as (app, base_dir):
            original = self._state(base_dir, refresh_id="a" * 32, request_id="b" * 32)
            from fls_manager.online_scripts.constants import update_online_refresh_state
            import fls_manager.routes.online_scripts.refresh as refresh

            update_online_refresh_state(
                running=False, status="失败", error="upstream timeout",
                cache_published=False, updated_at="now",
            )
            retry_request = "c" * 32
            client = app.test_client()
            with patch.object(refresh, "start_refresh_thread") as start:
                first = client.post(
                    f"/api/online-scripts/refresh-retry/{original}",
                    json={"retry_request_id": retry_request, "proxy_id": ""},
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(first.status_code, 202)
                self.assertEqual(first.json["retry_of"], original)
                replay = client.post(
                    f"/api/online-scripts/refresh-retry/{original}",
                    json={"retry_request_id": retry_request, "proxy_id": ""},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(replay.status_code, 200)
            self.assertTrue(replay.json["replayed"])
            self.assertEqual(start.call_count, 1)
            retry_status = client.get(
                f"/api/online-scripts/refresh-status/{retry_request}",
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(retry_status.status_code, 200)
            self.assertEqual(retry_status.json["retry_of"], original)
            page = client.get("/online-scripts", headers={"X-Token": TOKEN})
            html = page.get_data(as_text=True)
            self.assertIn(f"/online-scripts/refresh-retry/{original}", html)
            self.assertIn("明确重新同步", html)

    def test_refresh_retry_replay_returns_original_receipt_after_cache_changes(self):
        for status in ("已完成", "部分完成", "失败", "结果未知"):
            with self.subTest(status=status), isolated_app() as (app, base_dir):
                from fls_manager.online_scripts import source
                from fls_manager.online_scripts.constants import update_online_refresh_state
                from fls_manager.online_scripts.logs import online_refresh_log_file
                import fls_manager.routes.online_scripts.refresh as refresh

                original = self._state(base_dir)
                retry_request = "c" * 32
                client = app.test_client()
                endpoint = f"/api/online-scripts/refresh-retry/{original}"
                values = {"retry_request_id": retry_request, "proxy_id": ""}
                headers = {"X-Token": TOKEN}
                with patch.object(refresh, "start_refresh_thread") as start:
                    first = client.post(endpoint, json=values, headers=headers)
                    self.assertEqual(first.status_code, 202)
                    log_file = online_refresh_log_file(first.json["id"])
                    log_file.parent.mkdir(parents=True, exist_ok=True)
                    log_file.write_text("original retry result\n", encoding="utf-8")
                    source.save_online_script_cache([{
                        "id": "new", "name": "New cache", "type": "raw", "link": "https://example.invalid/new.py",
                    }])
                    update_online_refresh_state(
                        running=False, status=status, message="retry finished", error="original diagnosis",
                        cache_published=True, published_cache_fingerprint=source.online_script_cache_fingerprint(),
                        log_file=str(log_file), updated_at="later",
                    )
                    history = base_dir / "data" / "online_script_refresh_requests.json"
                    before = history.read_bytes()
                    replay = client.post(endpoint, json=values, headers=headers)
                    self.assertEqual(replay.status_code, 200)
                    self.assertTrue(replay.json["replayed"])
                    self.assertEqual(replay.json["id"], first.json["id"])
                    self.assertEqual(replay.json["status"], status)
                    with patch.object(refresh, "online_script_cache_fingerprint", side_effect=RuntimeError("cache unreadable")) as cache, patch.object(
                        refresh, "online_script_source_fingerprint", side_effect=RuntimeError("source unreadable"),
                    ) as config, patch.object(refresh, "validate_proxy_id", side_effect=ValueError("proxy deleted")) as proxy:
                        replay = client.post(endpoint, json=values, headers=headers)
                        self.assertEqual(replay.status_code, 200)
                        self.assertEqual(replay.json["error"], "original diagnosis")
                        self.assertEqual(replay.json["state_url"], f"/api/online-scripts/refresh-status/{retry_request}")
                        page = client.post(endpoint.replace("/api/", "/", 1), data=values, headers=headers)
                        self.assertEqual(page.status_code, 200)
                        self.assertIn("original diagnosis", page.get_data(as_text=True))
                        self.assertIn(f"/online-scripts/refresh-log/{retry_request}", page.get_data(as_text=True))
                        cache.assert_not_called()
                        config.assert_not_called()
                        proxy.assert_not_called()
                    stale = client.post(endpoint, json={**values, "retry_request_id": "d" * 32}, headers=headers)
                    self.assertEqual(stale.status_code, 409)
                    wrong_proxy = client.post(endpoint, json={**values, "proxy_id": "other"}, headers=headers)
                    self.assertEqual(wrong_proxy.status_code, 409)
                    wrong_parent = client.post("/api/online-scripts/refresh-retry/" + "e" * 32, json=values, headers=headers)
                    self.assertEqual(wrong_parent.status_code, 409)
                    self.assertEqual(history.read_bytes(), before)
                    self.assertEqual(start.call_count, 1)

    def test_refresh_retry_replay_survives_parent_cleanup_and_claim_race(self):
        with isolated_app() as (app, base_dir):
            import fls_manager.routes.online_scripts.refresh as refresh
            from fls_manager.online_scripts import source
            from fls_manager.online_scripts.constants import claim_online_refresh_retry, update_online_refresh_state

            original = self._state(base_dir)
            client = app.test_client()
            endpoint = f"/api/online-scripts/refresh-retry/{original}"
            values = {"retry_request_id": "c" * 32, "proxy_id": ""}
            headers = {"X-Token": TOKEN}
            with patch.object(refresh, "start_refresh_thread") as start:
                first = client.post(endpoint, json=values, headers=headers)
                self.assertEqual(first.status_code, 202)
                source.save_online_script_cache([{"id": "later", "name": "Later", "type": "raw", "link": "x"}])
                update_online_refresh_state(running=False, status="失败", error="child failure", updated_at="later")
                history = base_dir / "data" / "online_script_refresh_requests.json"
                records = json.loads(history.read_text(encoding="utf-8"))
                records.pop(original)
                history.write_text(json.dumps(records), encoding="utf-8")
                before = history.read_bytes()
                replay = client.post(endpoint, json=values, headers=headers)
                self.assertEqual(replay.status_code, 200)
                self.assertEqual(replay.json["id"], first.json["id"])
                cache_fingerprint = source.online_script_cache_fingerprint()
                source_fingerprint = source.online_script_source_fingerprint()

                def concurrent_claim():
                    claimed = claim_online_refresh_retry(
                        original, "", values["retry_request_id"], cache_fingerprint, source_fingerprint,
                    )
                    self.assertTrue(claimed["_replay"])
                    return cache_fingerprint

                with patch.object(refresh, "get_online_refresh_retry", return_value=None), patch.object(
                    refresh, "online_script_cache_fingerprint", side_effect=concurrent_claim,
                ), patch.object(refresh, "online_script_source_fingerprint", return_value=source_fingerprint):
                    raced = client.post(endpoint, json=values, headers=headers)
                self.assertEqual(raced.status_code, 200)
                self.assertEqual(raced.json["id"], first.json["id"])
                missing = client.post(endpoint, json={**values, "retry_request_id": "d" * 32}, headers=headers)
                self.assertEqual(missing.status_code, 404)
                with client.session_transaction() as session:
                    session["csrf_token"] = "retry-csrf"
                unauthenticated = client.post(endpoint, json=values, headers={"X-CSRF-Token": "retry-csrf"})
                self.assertEqual(unauthenticated.status_code, 401)
                with client.session_transaction() as session:
                    session.update(token=TOKEN, token_expire_at=4102444800, security_verified=True, csrf_token="retry-csrf")
                denied = client.post(endpoint, json=values)
                self.assertEqual(denied.status_code, 400)
                allowed = client.post(endpoint, json=values, headers={"X-CSRF-Token": "retry-csrf"})
                self.assertEqual(allowed.status_code, 200)
                self.assertEqual(history.read_bytes(), before)
                self.assertEqual(start.call_count, 1)
                history.write_text("{broken", encoding="utf-8")
                corrupt = client.post(endpoint, json=values, headers=headers)
                self.assertEqual(corrupt.status_code, 500)
                self.assertEqual(history.read_text(encoding="utf-8"), "{broken")
                self.assertEqual(start.call_count, 1)

    def test_refresh_retry_concurrent_preflight_claims_one_child(self):
        with isolated_app() as (app, base_dir):
            import fls_manager.routes.online_scripts.refresh as refresh
            from fls_manager.online_scripts.source import online_script_cache_fingerprint

            original = self._state(base_dir)
            fingerprint = online_script_cache_fingerprint()
            barrier = threading.Barrier(2)
            results, errors = [], []

            def preflight():
                barrier.wait(timeout=5)
                return fingerprint

            def submit():
                try:
                    response = app.test_client().post(
                        f"/api/online-scripts/refresh-retry/{original}",
                        json={"retry_request_id": "c" * 32, "proxy_id": ""}, headers={"X-Token": TOKEN},
                    )
                    results.append((response.status_code, response.json))
                except Exception as exc:
                    errors.append(exc)

            with patch.object(refresh, "online_script_cache_fingerprint", side_effect=preflight), patch.object(
                refresh, "start_refresh_thread",
            ) as start:
                threads = [threading.Thread(target=submit) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)
                self.assertFalse(any(thread.is_alive() for thread in threads))
                self.assertEqual(errors, [])
                self.assertEqual(sorted(code for code, _ in results), [200, 202])
                self.assertEqual(len({payload["id"] for _, payload in results}), 1)
                self.assertEqual(start.call_count, 1)

    def test_refresh_retry_rejects_terminal_or_bound_conflicts_without_starting(self):
        with isolated_app() as (app, base_dir):
            original = self._state(base_dir, refresh_id="a" * 32, request_id="b" * 32)
            from fls_manager.online_scripts.constants import update_online_refresh_state
            import fls_manager.routes.online_scripts.refresh as refresh
            client = app.test_client()
            update_online_refresh_state(
                running=False, status="已完成", error="", cache_published=True,
                updated_at="now",
            )
            with patch.object(refresh, "start_refresh_thread") as start:
                completed = client.post(
                    f"/api/online-scripts/refresh-retry/{original}",
                    json={"retry_request_id": "c" * 32, "proxy_id": ""},
                    headers={"X-Token": TOKEN},
                )
                invalid = client.post(
                    "/api/online-scripts/refresh-retry/" + "d" * 32,
                    json={"retry_request_id": "e" * 32, "proxy_id": ""},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(completed.status_code, 409)
            self.assertEqual(invalid.status_code, 404)
            self.assertEqual(start.call_count, 0)
            update_online_refresh_state(
                running=False, status="失败", error="retryable",
                cache_published=False, updated_at="later",
            )
            with patch.object(refresh, "start_refresh_thread") as start:
                conflict = client.post(
                    f"/api/online-scripts/refresh-retry/{original}",
                    json={"retry_request_id": "f" * 32, "proxy_id": "missing"},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(conflict.status_code, 400)
            self.assertEqual(start.call_count, 0)

    def test_refresh_retry_form_requires_csrf_and_returns_recoverable_error(self):
        with isolated_app() as (app, base_dir):
            original = self._state(base_dir)
            from fls_manager.online_scripts.constants import update_online_refresh_state

            update_online_refresh_state(
                running=False, status="失败", error="upstream timeout",
                cache_published=False, updated_at="now",
            )
            client = app.test_client()
            with client.session_transaction() as session:
                session.update(token=TOKEN, token_expire_at=4102444800, security_verified=True, csrf_token="retry-csrf")
            denied = client.post(
                f"/online-scripts/refresh-retry/{original}",
                data={"retry_request_id": "c" * 32, "proxy_id": ""},
            )
            self.assertIn(denied.status_code, (400, 403))
            html = denied.get_data(as_text=True)
            self.assertIn("CSRF", html)
            csrf = "retry-csrf"
            with patch("fls_manager.routes.online_scripts.refresh.start_refresh_thread"):
                result = client.post(
                    f"/online-scripts/refresh-retry/{original}",
                    data={"retry_request_id": "c" * 32, "proxy_id": "", "csrf_token": csrf},
                )
            self.assertIn(result.status_code, (302, 500))

    def test_unknown_refresh_result_confirms_published_fingerprint_idempotently(self):
        with isolated_app() as (app, base_dir):
            request_id = self._state(base_dir)
            from fls_manager.online_scripts.constants import update_online_refresh_state
            from fls_manager.online_scripts.source import online_script_cache_fingerprint, online_script_source_fingerprint

            fingerprint = online_script_cache_fingerprint()
            update_online_refresh_state(
                running=False, status="结果未知", error="回执写入失败",
                cache_published=True, published_cache_fingerprint=fingerprint,
                source_fingerprint=online_script_source_fingerprint(), updated_at="later",
            )
            client = app.test_client()
            status_page = client.get("/online-scripts", headers={"X-Token": TOKEN})
            self.assertIn(f"/online-scripts/refresh-confirm/{request_id}", status_page.get_data(as_text=True))
            log_page = client.get(f"/online-scripts/refresh-log/{request_id}", headers={"X-Token": TOKEN})
            self.assertIn("检查当前缓存结果", log_page.get_data(as_text=True))
            confirmed = client.post(
                f"/api/online-scripts/refresh-confirm/{request_id}", headers={"X-Token": TOKEN},
            )
            self.assertEqual(confirmed.status_code, 200)
            self.assertEqual(confirmed.json["status"], "已确认发布")
            log_payload = client.get(
                f"/api/online-scripts/refresh-log/{request_id}", headers={"X-Token": TOKEN},
            )
            self.assertTrue(log_payload.json["terminal"])
            replay = client.post(
                f"/api/online-scripts/refresh-confirm/{request_id}", headers={"X-Token": TOKEN},
            )
            self.assertEqual(replay.status_code, 200)
            self.assertTrue(replay.json["replayed"])
            status = client.get(
                f"/api/online-scripts/refresh-status/{request_id}", headers={"X-Token": TOKEN},
            )
            self.assertEqual(status.json["confirmation"], "published")

    def test_unknown_refresh_confirmation_rejects_later_cache_change_without_mutation(self):
        with isolated_app() as (app, base_dir):
            request_id = self._state(base_dir)
            from fls_manager.online_scripts import source
            from fls_manager.online_scripts.constants import update_online_refresh_state

            old_fingerprint = source.online_script_cache_fingerprint()
            update_online_refresh_state(
                running=False, status="结果未知", error="回执写入失败",
                cache_published=True, published_cache_fingerprint=old_fingerprint,
                updated_at="later",
            )
            source.save_online_script_cache([{"id": "later", "name": "Later", "type": "raw", "link": "x"}])
            client = app.test_client()
            response = client.post(
                f"/api/online-scripts/refresh-confirm/{request_id}", headers={"X-Token": TOKEN},
            )
            self.assertEqual(response.status_code, 409)
            self.assertEqual(client.get(
                f"/api/online-scripts/refresh-status/{request_id}", headers={"X-Token": TOKEN},
            ).json["status"], "结果未知")
            self.assertEqual(source.load_online_script_cache()[0]["id"], "later")

    def test_unknown_refresh_confirmation_can_confirm_not_published_baseline(self):
        with isolated_app() as (app, base_dir):
            request_id = self._state(base_dir)
            from fls_manager.online_scripts.constants import update_online_refresh_state
            from fls_manager.online_scripts.source import online_script_cache_fingerprint, online_script_source_fingerprint

            update_online_refresh_state(
                running=False, status="结果未知", error="未确认是否发布",
                cache_published=False, expected_cache_fingerprint=online_script_cache_fingerprint(),
                source_fingerprint=online_script_source_fingerprint(), updated_at="later",
            )
            response = app.test_client().post(
                f"/api/online-scripts/refresh-confirm/{request_id}", headers={"X-Token": TOKEN},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["status"], "已确认未发布")

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise refresh log polling")
    def test_refresh_log_page_polls_running_state_and_stops_at_terminal(self):
        with isolated_app() as (app, base_dir):
            request_id = self._state(base_dir)
            from fls_manager.online_scripts.constants import update_online_refresh_state

            update_online_refresh_state(running=True, status="运行中", message="still working")
            html = app.test_client().get(
                f"/online-scripts/refresh-log/{request_id}", headers={"X-Token": TOKEN},
            ).get_data(as_text=True)
            script = next(script for script in re.findall(r"<script>(.*?)</script>", html, re.S) if "schedulePolling" in script)
            static = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
            reader = static[static.index("async function flsReadJsonResponse("):static.index("async function flsReadTextResponse(")]
            result = subprocess.run(
                ["node", "-e", r'''
const vm = require('node:vm');
const assert = require('node:assert/strict');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
(async()=>{
 const elements={refreshLogError:{hidden:true,textContent:''},refreshLogLogin:{hidden:true,href:''},refreshLogRetry:{disabled:false},refreshLogText:{textContent:'old'},refreshLogStatus:{textContent:'运行中'},refreshLogUpdatedAt:{textContent:'old'},refreshLogConfirm:{hidden:true},refreshLogHistory:{hidden:true},refreshLogDiagnosis:{hidden:true,textContent:''},refreshLogWaiting:{hidden:true,textContent:''}};
 const timers=[]; let pagehide=null; let calls=0; let click=null;
 const responses=[
   {ok:true,status:200,body:{ok:true,log:'暂无日志',status:'运行中',updated_at:'zero',running:true,terminal:false,log_pending:true,log_status:404}},
   {ok:true,status:200,body:{ok:true,log:'first line',status:'运行中',updated_at:'one',running:true,terminal:false,log_pending:false,log_status:200}},
   {ok:true,status:200,body:{ok:true,log:'final line',status:'已完成',updated_at:'two',running:false,terminal:true,log_pending:false,log_status:200}},
 ];
 const fakeSetTimeout=fn=>{timers.push(fn);return fn;};
 const context={document:{getElementById:id=>elements[id]},window:{addEventListener:(name,fn)=>{if(name==='pagehide') pagehide=fn;}},location:{pathname:'/online-scripts/refresh-log/'+('b'.repeat(32)),search:''},encodeURIComponent,
   fetch:async()=>{const item=responses[calls++];return {ok:item.ok,status:item.status,headers:{get:()=> 'application/json'},text:async()=>JSON.stringify(item.body)};},
   setTimeout:fakeSetTimeout,clearTimeout:fn=>{const index=timers.indexOf(fn);if(index>=0)timers.splice(index,1);},
   flsResponseErrorDiagnostic:(_r,b)=>({payload:JSON.parse(b),message:'backend diagnostic'})};
 elements.refreshLogRetry.addEventListener=(name,fn)=>{if(name==='click') click=fn;};
 vm.createContext(context);vm.runInContext(input.reader+input.script,context);
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(calls,1); assert.equal(timers.length,1); assert.equal(elements.refreshLogText.textContent,'暂无日志');
 assert.equal(elements.refreshLogWaiting.hidden,false);
 const timer=timers.shift(); timer(); await new Promise(resolve=>setImmediate(resolve));
 assert.equal(calls,2); assert.equal(timers.length,1); assert.equal(elements.refreshLogText.textContent,'first line');
 assert.equal(elements.refreshLogWaiting.hidden,true);
 const completionTimer=timers.shift(); completionTimer(); await new Promise(resolve=>setImmediate(resolve));
 assert.equal(calls,3); assert.equal(timers.length,0); assert.equal(elements.refreshLogText.textContent,'final line');
 assert.equal(elements.refreshLogStatus.textContent,'已完成'); assert.equal(typeof pagehide,'function');
 assert.equal(elements.refreshLogConfirm.hidden,true); assert.equal(elements.refreshLogHistory.hidden,true);
 responses.push({ok:true,status:200,body:{ok:true,log:'original interrupted log',status:'结果未知',running:false,terminal:true,error:'original recovery diagnosis',log_pending:false,log_status:200}});
 await click();
 assert.equal(calls,4); assert.equal(timers.length,0);
 assert.equal(elements.refreshLogConfirm.hidden,false); assert.equal(elements.refreshLogHistory.hidden,false);
 assert.equal(elements.refreshLogDiagnosis.hidden,false); assert.equal(elements.refreshLogDiagnosis.textContent,'original recovery diagnosis');
 assert.equal(elements.refreshLogText.textContent,'original interrupted log');
 console.log('refresh log polling passed');
})().catch(error=>{console.error(error);process.exitCode=1});
'''], input=json.dumps({"script": script, "reader": reader}), text=True, capture_output=True, timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise refresh log retry backoff")
    def test_refresh_log_retries_transient_errors_with_bounded_backoff(self):
        with isolated_app() as (app, base_dir):
            request_id = self._state(base_dir)
            from fls_manager.online_scripts.constants import update_online_refresh_state

            update_online_refresh_state(running=True, status="运行中", message="still working", error="")
            html = app.test_client().get(
                f"/online-scripts/refresh-log/{request_id}", headers={"X-Token": TOKEN},
            ).get_data(as_text=True)
            script = next(script for script in re.findall(r"<script>(.*?)</script>", html, re.S) if "schedulePolling" in script)
            static = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
            reader = static[static.index("async function flsReadJsonResponse("):static.index("async function flsReadTextResponse(")]
            result = subprocess.run(
                ["node", "-e", r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const settle=async()=>{await new Promise(resolve=>setImmediate(resolve));await new Promise(resolve=>setImmediate(resolve));};
const ok=(status,log,terminal=false)=>({status:200,body:{ok:true,log,status,updated_at:'now',running:!terminal,terminal,log_pending:false}});
const http=(status,body={error:'temporary gateway error'})=>({status,body});
function setup(queue){
 const elements={refreshLogError:{hidden:true,textContent:''},refreshLogLogin:{hidden:true,href:''},refreshLogRetry:{disabled:false},refreshLogText:{textContent:'last valid log'},refreshLogStatus:{textContent:'运行中'},refreshLogUpdatedAt:{textContent:'old'},refreshLogConfirm:{hidden:true},refreshLogHistory:{hidden:true},refreshLogDiagnosis:{hidden:true,textContent:''},refreshLogWaiting:{hidden:true,textContent:''}};
 const timers=[];let calls=0,click=null,pagehide=null;
 const context={document:{getElementById:id=>elements[id]},window:{addEventListener:(name,fn)=>{if(name==='pagehide')pagehide=fn;}},location:{pathname:'/online-scripts/refresh-log/'+('b'.repeat(32)),search:''},encodeURIComponent,
  fetch:async()=>{calls++;const item=queue.shift();if(item.network)throw new TypeError('network offline');return {ok:item.status===200,status:item.status,headers:{get:()=> 'application/json'},text:async()=>JSON.stringify(item.body)};},
  setTimeout:(fn,delay)=>{const timer={fn,delay};timers.push(timer);return timer;},
  clearTimeout:timer=>{const index=timers.indexOf(timer);if(index>=0)timers.splice(index,1);},
  flsResponseErrorDiagnostic:(_response,body)=>({payload:JSON.parse(body),message:JSON.parse(body).error||''})};
 elements.refreshLogRetry.addEventListener=(_name,fn)=>{click=fn;};
 vm.createContext(context);vm.runInContext(input.reader+input.script,context);
 return {elements,timers,context,calls:()=>calls,click:()=>click(),pagehide:()=>pagehide()};
}
(async()=>{
 const recovery=setup([http(503),{network:true},http(429),http(502),ok('运行中','recovered log'),ok('已完成','final log',true)]);
 await settle();
 assert.equal(recovery.calls(),1);assert.equal(recovery.timers[0].delay,2000);
 assert.match(recovery.elements.refreshLogError.textContent,/1\/5/);
 assert.equal(recovery.elements.refreshLogText.textContent,'last valid log');
 let timer=recovery.timers.shift();timer.fn();await settle();
 assert.equal(recovery.calls(),2);assert.equal(recovery.timers[0].delay,4000);
 assert.match(recovery.elements.refreshLogError.textContent,/2\/5/);
 await recovery.click();await settle();
 assert.equal(recovery.calls(),3);assert.equal(recovery.timers.length,1);
 assert.equal(recovery.timers[0].delay,2000);
 assert.match(recovery.elements.refreshLogError.textContent,/1\/5/);
 timer=recovery.timers.shift();timer.fn();await settle();
 assert.equal(recovery.calls(),4);assert.equal(recovery.timers[0].delay,4000);
 timer=recovery.timers.shift();timer.fn();await settle();
 assert.equal(recovery.calls(),5);assert.equal(recovery.elements.refreshLogText.textContent,'recovered log');
 assert.equal(recovery.elements.refreshLogError.hidden,true);
 assert.equal(recovery.timers[0].delay,2000);
 timer=recovery.timers.shift();timer.fn();await settle();
 assert.equal(recovery.calls(),6);assert.equal(recovery.elements.refreshLogText.textContent,'final log');
 assert.equal(recovery.elements.refreshLogStatus.textContent,'已完成');assert.equal(recovery.timers.length,0);

 for(const status of [401,403,404]){
  const body=status===404?{error:'terminal log missing',refresh_state:{running:false,status:'已完成'}}:{error:'request denied'};
  const stopped=setup([http(status,body)]);await settle();
  assert.equal(stopped.calls(),1);assert.equal(stopped.timers.length,0);
  assert.equal(stopped.elements.refreshLogLogin.hidden,!(status===401||status===403));
 }

 const bounded=setup([...Array.from({length:6},()=>http(503)),ok('运行中','manual recovery')]);
 await settle();
 const delays=[];
 while(bounded.timers.length){timer=bounded.timers.shift();delays.push(timer.delay);timer.fn();await settle();}
 assert.deepEqual(delays,[2000,4000,8000,16000,30000]);
 assert.equal(bounded.calls(),6);assert.equal(bounded.timers.length,0);
 assert.match(bounded.elements.refreshLogError.textContent,/次数已达上限/);
 await bounded.click();await settle();
 assert.equal(bounded.calls(),7);assert.equal(bounded.timers.length,1);
 assert.equal(bounded.timers[0].delay,2000);
 assert.equal(bounded.elements.refreshLogText.textContent,'manual recovery');
 bounded.pagehide();
 assert.equal(bounded.timers.length,0);
 console.log('transient refresh log retry/backoff passed');
})().catch(error=>{console.error(error);process.exitCode=1});
'''], input=json.dumps({"script": script, "reader": reader}), text=True, capture_output=True, timeout=8,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise refresh log recovery")
    def test_refresh_log_page_restores_retry_and_login_actions_on_http_errors(self):
        with isolated_app() as (app, base_dir):
            request_id = self._state(base_dir)
            html = app.test_client().get(
                f"/online-scripts/refresh-log/{request_id}", headers={"X-Token": TOKEN},
            ).get_data(as_text=True)
            script = next(script for script in re.findall(r"<script>(.*?)</script>", html, re.S) if "loadRefreshLog" in script)
            static = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
            reader = static[static.index("async function flsReadJsonResponse("):static.index("async function flsReadTextResponse(")]
            result = subprocess.run(
                ["node", "-e", r'''
const vm = require('node:vm');
const assert = require('node:assert/strict');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
(async()=>{
 for(const status of [401,403,404,500,502]){
  const elements={refreshLogError:{hidden:true,textContent:''},refreshLogLogin:{hidden:true,href:''},refreshLogRetry:{disabled:false},refreshLogText:{textContent:'old'},refreshLogStatus:{textContent:'失败'},refreshLogUpdatedAt:{textContent:'old'}};
  let click=null;
  const context={document:{getElementById:id=>elements[id]},window:{addEventListener:()=>{}},location:{pathname:'/online-scripts/refresh-log/'+('b'.repeat(32)),search:''},encodeURIComponent,
    fetch:async()=>({ok:false,status,headers:{get:()=> 'application/json'},text:async()=>JSON.stringify({error:'backend diagnostic'})}),
    setTimeout,flsResponseErrorDiagnostic:(_r,b)=>({payload:JSON.parse(b),message:'backend diagnostic'})};
  elements.refreshLogRetry.addEventListener=(name,fn)=>{if(name==='click') click=fn;};
  vm.createContext(context);vm.runInContext(input.reader+input.script,context);await new Promise(resolve=>setImmediate(resolve));
  await click();
  assert.equal(elements.refreshLogRetry.disabled,false);
  assert.match(elements.refreshLogError.textContent,/刷新日志读取失败/);
  assert.equal(elements.refreshLogLogin.hidden,!(status===401||status===403));
 }
 console.log('refresh log error recovery passed');
})().catch(error=>{console.error(error);process.exitCode=1});
'''], input=json.dumps({"script": script, "reader": reader}), text=True, capture_output=True, timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
