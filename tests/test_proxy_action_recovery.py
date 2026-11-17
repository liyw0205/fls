import importlib
import shlex
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from tests.test_bulk_workflows import TOKEN, isolated_app, sample_task
from tests.test_collection_save_recovery import CollectionFormParser


class ProxyActionRecoveryTests(unittest.TestCase):
    def seed(self):
        from fls_manager import models
        models.save_proxies([{"id": "p", "name": "Original", "type": "http", "host": "127.0.0.1", "port": "8080", "password": "secret", "enabled": True},
                             {"id": "other", "name": "Unrelated", "enabled": True}])

    def values(self, action="toggle", **extra):
        from fls_manager import models
        from fls_manager.proxy_actions import proxy_version
        return dict({"action": action, "proxy_id": "p", "version": proxy_version(models.load_proxies()[0]),
                     "request_id": "a" * 32, "enabled": False}, **extra)

    def post(self, client, data):
        return client.post("/api/proxy/action", json=data, headers={"X-Token": TOKEN})

    def authenticate(self, client):
        with client.session_transaction() as session:
            session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                           security_verified=True, csrf_token="proxy-csrf")

    def form(self, response, action):
        parser = CollectionFormParser(response.get_data(as_text=True))
        return next(form for form in parser.forms if form["action"].startswith(action))

    def test_explicit_intent_restart_replay_preserves_later_changes(self):
        for action in ("toggle", "delete"):
            with self.subTest(action=action), isolated_app() as (app, base):
                from fls_manager import models, proxy_actions
                self.seed()
                data = self.values(action)
                client = app.test_client()
                self.assertEqual(self.post(client, data).status_code, 200)
                proxies = models.load_proxies()
                self.assertEqual([p["id"] for p in proxies], ["other"] if action == "delete" else ["p", "other"])
                if action == "toggle":
                    self.assertFalse(proxies[0]["enabled"])
                self.assertNotIn("secret", (base / "data/proxy_requests" / ("a" * 32 + ".json")).read_text())
                self.seed()
                importlib.reload(proxy_actions)
                with patch.object(models, "save_proxies") as save:
                    self.assertEqual(self.post(client, data).status_code, 200)
                    self.assertEqual(self.post(client, dict(data, version="f" * 64)).status_code, 409)
                    save.assert_not_called()
                self.assertTrue(models.load_proxies()[0]["enabled"])

    def test_invalid_missing_stale_and_referenced_have_zero_writes(self):
        for kind in ("missing", "stale", "enabled", "request_id", "version", "reference", "delete_reference"):
            with self.subTest(kind=kind), isolated_app() as (app, base):
                from fls_manager import models
                self.seed()
                data = self.values("delete" if kind == "delete_reference" else "toggle")
                if kind == "missing":
                    data["proxy_id"] = "missing"
                elif kind == "stale":
                    data["version"] = "f" * 64
                elif kind in ("enabled", "request_id", "version"):
                    data[kind] = [False]
                else:
                    models.save_tasks([sample_task("t", proxy_id="p")])
                before = (base / "data/proxies.json").read_bytes()
                with patch.object(models, "save_proxies") as save:
                    response = self.post(app.test_client(), data)
                    self.assertIn(response.status_code, (400, 404, 409))
                    save.assert_not_called()
                self.assertEqual((base / "data/proxies.json").read_bytes(), before)
                self.assertFalse((base / "data/proxy_requests").exists())

    def test_publication_before_after_and_failed_confirmation_real_state(self):
        for action in ("toggle", "delete"):
            for published in (False, True):
                with self.subTest(action=action, published=published), isolated_app() as (app, _):
                    from fls_manager import models, paths
                    self.seed()
                    data = self.values(action)
                    original = models.load_proxies()
                    replace = Path.replace
                    def fail(path, target):
                        if target == paths.PROXY_FILE:
                            if published:
                                replace(path, target)
                            raise OSError("publication failed")
                        return replace(path, target)
                    with patch.object(Path, "replace", fail):
                        response = self.post(app.test_client(), data)
                    self.assertEqual(response.status_code, 503 if published else 500)
                    self.assertIs(response.json["saved"], published)
                    self.assertEqual(models.load_proxies() == original, not published)
                    self.assertEqual(self.post(app.test_client(), data).status_code, 200)

    def test_receipts_interruption_and_external_change_recovery(self):
        for mode in ("prepared", "published", "changed", "corrupt", "unreadable"):
            with self.subTest(mode=mode), isolated_app() as (app, base):
                from fls_manager import models, proxy_actions
                self.seed()
                data = self.values()
                save, write = models.save_proxies, proxy_actions.write_json
                def receipt(path, row):
                    if row["state"] != "prepared":
                        raise OSError("receipt failed")
                    return write(path, row)
                def persist(values):
                    if mode == "published":
                        return save(values)
                    raise OSError("disk full")
                with patch.object(proxy_actions, "write_json", side_effect=receipt), patch.object(models, "save_proxies", side_effect=persist):
                    response = self.post(app.test_client(), data)
                self.assertEqual(response.status_code, 503 if mode == "published" else 500)
                record = base / "data/proxy_requests" / ("a" * 32 + ".json")
                if mode == "changed":
                    proxies = models.load_proxies()
                    proxies[0]["name"] = "Changed"
                    save(proxies)
                elif mode == "corrupt":
                    record.write_text("{", encoding="utf-8")
                with patch.object(Path, "read_text", side_effect=PermissionError("denied")) if mode == "unreadable" else patch.object(models, "save_proxies", wraps=save) as saved:
                    response = self.post(app.test_client(), data)
                self.assertEqual(response.status_code, {"prepared": 409, "published": 200, "changed": 409, "corrupt": 500, "unreadable": 500}[mode])
                if mode == "prepared":
                    self.assertTrue(response.json["confirm_required"])
                    self.assertEqual(self.post(app.test_client(), dict(data, confirm_retry=True)).status_code, 200)
                elif mode == "changed":
                    self.assertIsNone(response.json["saved"])
                    self.assertTrue(models.load_proxies()[0]["enabled"])

    def test_concurrent_replay_and_unrelated_create_do_not_lose_changes(self):
        with isolated_app() as (app, _):
            from fls_manager import models
            self.seed()
            data = self.values()
            entered, release = threading.Event(), threading.Event()
            save = models.save_proxies
            def delayed(values):
                entered.set()
                self.assertTrue(release.wait(5))
                return save(values)
            def create():
                client = app.test_client()
                self.authenticate(client)
                return client.post("/proxy/new", data={"name": "Concurrent", "enabled": "1", "csrf_token": "proxy-csrf"})
            with patch.object(models, "save_proxies", side_effect=delayed) as write, ThreadPoolExecutor(3) as pool:
                first = pool.submit(self.post, app.test_client(), data)
                self.assertTrue(entered.wait(5))
                second = pool.submit(self.post, app.test_client(), data)
                third = pool.submit(create)
                release.set()
                self.assertEqual([future.result(5).status_code for future in (first, second, third)], [200, 200, 302])
                self.assertEqual(write.call_count, 1)
            self.assertEqual(len(models.load_proxies()), 3)

    def test_get_auth_csrf_and_real_forms_recover_without_toggling_twice(self):
        with isolated_app() as (app, _):
            from fls_manager import models
            from fls_manager.routes.proxy import actions
            self.seed()
            client = app.test_client()
            data = self.values()
            for headers, status in (({}, 400), ({"X-Token": "wrong"}, 400)):
                self.assertEqual(client.post("/api/proxy/action", json=data, headers=headers).status_code, status)
            self.authenticate(client)
            with client.session_transaction() as session:
                session["token_expire_at"] = 1
            self.assertEqual(client.post("/api/proxy/action", json=data, headers={"X-CSRF-Token": "proxy-csrf"}).status_code, 401)
            self.authenticate(client)
            before = models.load_proxies()
            self.assertEqual(client.get("/proxy/toggle/p").status_code, 302)
            self.assertEqual(models.load_proxies(), before)
            self.assertEqual(client.post("/api/proxy/action", json=data).status_code, 400)
            form = self.form(client.get("/proxy"), "/proxy/toggle/p")
            self.assertEqual(form["fields"]["enabled"], "0")
            self.assertEqual(client.post(form["action"], data=dict(form["fields"], csrf_token="wrong")).status_code, 400)
            with patch.object(actions, "save_proxies", side_effect=OSError("full")):
                response = client.post(form["action"], data=form["fields"], headers={"X-Requested-With": "XMLHttpRequest"})
            self.assertEqual(response.status_code, 500)
            retry = self.form(response, "/proxy/toggle/p")
            self.assertEqual(retry["fields"]["request_id"], form["fields"]["request_id"])
            self.assertEqual(client.post(retry["action"], data=retry["fields"]).status_code, 302)
            self.assertEqual(client.post(form["action"], data=form["fields"]).status_code, 302)
            self.assertFalse(models.load_proxies()[0]["enabled"])

    def test_damaged_proxy_storage_never_overwritten(self):
        for content in ("{", "{}", '[null]', '[{"id":"p"},"bad row"]', '[{"id":"duplicate"},{"id":"duplicate"}]'):
            with self.subTest(content=content), isolated_app() as (app, base):
                from fls_manager import models, paths
                self.seed()
                data = self.values()
                paths.PROXY_FILE.write_text(content, encoding="utf-8")
                self.assertEqual(self.post(app.test_client(), data).status_code, 500)
                self.assertEqual(paths.PROXY_FILE.read_text(), content)

    def test_references_added_after_failure_and_edit_cannot_bypass_boundary(self):
        with isolated_app() as (app, _):
            from fls_manager import models
            self.seed()
            client = app.test_client()
            self.authenticate(client)
            form = self.form(client.get("/proxy/edit/p"), "/proxy/edit/p")
            data = self.values("delete")
            with patch.object(models, "save_proxies", side_effect=OSError("full")):
                self.assertEqual(self.post(client, data).status_code, 500)
            models.save_tasks([sample_task("t", proxy_id="p")])
            self.assertEqual(self.post(client, data).status_code, 409)
            form["fields"].pop("enabled", None)
            response = client.post(form["action"], data=form["fields"])
            self.assertEqual(response.status_code, 409)
            self.assertIn("任务引用", response.get_data(as_text=True))
            self.assertTrue(models.load_proxies()[0]["enabled"])
            proxies = models.load_proxies()
            proxies[0]["host"] = "later.example"
            models.save_proxies(proxies)
            self.assertEqual(client.post(form["action"], data=form["fields"]).status_code, 409)
            self.assertEqual(models.load_proxies()[0]["host"], "later.example")

    def test_task_api_fails_closed_then_actual_process_uses_restored_proxy(self):
        for state in ("missing", "disabled", "invalid"):
            with self.subTest(state=state), isolated_app() as (app, base):
                from fls_manager import models, task_runner, proxy
                self.seed()
                proxies = models.load_proxies()
                if state == "missing":
                    proxies = proxies[1:]
                elif state == "disabled":
                    proxies[0]["enabled"] = False
                else:
                    proxies[0]["port"] = ""
                models.save_proxies(proxies)
                marker = base / "actual-proxy.txt"
                code = "import os,pathlib;pathlib.Path(" + repr(str(marker)) + ").write_text(os.environ['HTTP_PROXY'])"
                models.save_tasks([sample_task("run", proxy_id="p", command=shlex.quote(sys.executable) + " -c " + shlex.quote(code))])
                client = app.test_client()
                with patch.object(task_runner.subprocess, "Popen") as process:
                    response = client.post("/api/task/action/run/run", json={"request_id": "b" * 32}, headers={"X-Token": TOKEN})
                    self.assertEqual(response.status_code, 500)
                    self.assertEqual(response.json["recovery_url"], "/proxy")
                    process.assert_not_called()
                self.authenticate(client)
                response = client.post("/run/run", data={"request_id": "f" * 32, "csrf_token": "proxy-csrf"})
                self.assertEqual(response.status_code, 500)
                self.assertIn('href="/proxy">修复所选代理', response.get_data(as_text=True))
                self.assertIn('href="/task/edit/run">更换任务代理', response.get_data(as_text=True))
                self.assertFalse(marker.exists())
                self.assertIn('value="p" selected', proxy.proxy_select_options("p"))
                self.seed()
                response = client.post("/api/task/action/run/run", json={"request_id": "c" * 32}, headers={"X-Token": TOKEN})
                self.assertEqual(response.status_code, 200)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and (not marker.exists() or task_runner.is_running("run")):
                    time.sleep(0.02)
                self.assertEqual(marker.read_text(), "http://127.0.0.1:8080")
                self.assertEqual(models.load_task_history()[0]["status"], "success")

    def test_worker_rechecks_proxy_after_submission_without_running_process(self):
        with isolated_app() as (app, _):
            from fls_manager import models, task_runner
            self.seed()
            models.save_tasks([sample_task("run", proxy_id="p", command="echo forbidden")])
            original = task_runner.apply_proxy_env
            calls = []
            def changed(env, proxy_id):
                calls.append(proxy_id)
                if len(calls) > 1:
                    raise ValueError("proxy removed after submission")
                return original(env, proxy_id)
            with patch.object(task_runner, "apply_proxy_env", side_effect=changed), patch.object(task_runner.subprocess, "Popen") as process:
                response = app.test_client().post("/api/task/action/run/run", json={"request_id": "d" * 32}, headers={"X-Token": TOKEN})
                self.assertEqual(response.status_code, 200)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and task_runner.is_running("run"):
                    time.sleep(0.02)
                process.assert_not_called()
            history = models.load_task_history()[0]
            self.assertEqual(history["status"], "start_failed")
            self.assertIn("proxy removed", history["message"])

    def test_proxy_disappearing_during_delay_is_checked_at_process_launch(self):
        with isolated_app() as (app, _):
            from fls_manager import models, task_runner
            self.seed()
            models.save_tasks([sample_task("run", proxy_id="p", command="echo forbidden")])
            calls = []
            original = task_runner.apply_proxy_env
            def changed(env, proxy_id):
                calls.append(proxy_id)
                if len(calls) > 2:
                    raise ValueError("proxy changed during delay")
                return original(env, proxy_id)
            with patch.object(task_runner, "apply_proxy_env", side_effect=changed), patch.object(task_runner.subprocess, "Popen") as process:
                response = app.test_client().post("/api/task/action/run/run", json={"request_id": "e" * 32}, headers={"X-Token": TOKEN})
                self.assertEqual(response.status_code, 200)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and task_runner.is_running("run"):
                    time.sleep(0.02)
                process.assert_not_called()
            self.assertEqual(models.load_task_history()[0]["status"], "start_failed")

    def test_initial_receipt_failure_and_unreadable_saved_state_do_not_claim_success(self):
        for mode in ("receipt", "verification"):
            with self.subTest(mode=mode), isolated_app() as (app, base):
                from fls_manager import models, paths, proxy_actions
                self.seed()
                data = self.values()
                before = paths.PROXY_FILE.read_bytes()
                if mode == "receipt":
                    with patch.object(proxy_actions, "write_json", side_effect=OSError("full")), patch.object(models, "save_proxies") as save:
                        response = self.post(app.test_client(), data)
                        save.assert_not_called()
                    self.assertIs(response.json["saved"], False)
                    self.assertTrue(response.json["confirm_required"])
                    self.assertEqual(paths.PROXY_FILE.read_bytes(), before)
                else:
                    original = models.load_proxies
                    def persist(values):
                        save(values)
                        raise OSError("published but failed")
                    save = models.save_proxies
                    with patch.object(models, "save_proxies", side_effect=persist), patch.object(models, "load_proxies", side_effect=[original(), PermissionError("denied")]):
                        response = self.post(app.test_client(), data)
                    self.assertIsNone(response.json["saved"])
                self.assertEqual(response.status_code, 500)
                self.assertEqual(self.post(app.test_client(), dict(data, confirm_retry=True)).status_code, 200)
