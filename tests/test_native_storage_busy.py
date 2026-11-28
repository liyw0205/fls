import contextlib
import time
import unittest
from unittest.mock import patch

from flask import request

from tests import test_backup_data_access as access_tests
from tests import test_native_env_api as env_tests
from tests import test_native_notify_api as notify_tests
from tests import test_native_proxy_api as proxy_tests
from tests.test_bulk_workflows import TOKEN
from tests.test_notify_action_recovery import isolated_app


class NativeStorageBusyTests(unittest.TestCase):
    def prepare(self, app):
        from fls_manager.native_sessions import create_native_session

        env_tests.NativeEnvApiTests().seed()
        proxy_tests.NativeProxyApiTests().seed()
        notify_tests.NativeNotifyApiTests().seed()
        with app.app_context():
            session_id, csrf, _ = create_native_session(TOKEN, remember=True, verified=True)
        client = app.test_client()
        client.set_cookie("fls_native_session", session_id)
        return client, {"X-CSRF-Token": csrf}

    def restore_after_auth(self, app):
        owner = {"stack": None, "endpoint": None}

        @app.before_request
        def begin_restore():
            if owner["stack"] is not None and owner["endpoint"] is None:
                body = "with directory_access(exclusive=True):\n print('ready',flush=True)\n sys.stdin.readline()\n"
                owner["stack"].enter_context(access_tests.BackupDataAccessTests().paused_process(body))
                owner["endpoint"] = request.endpoint

        @contextlib.contextmanager
        def restoring():
            with contextlib.ExitStack() as stack:
                owner.update(stack=stack, endpoint=None)
                try:
                    yield owner
                    self.assertIsNotNone(owner["endpoint"], "request never passed authentication")
                finally:
                    owner["stack"] = None

        return restoring

    def assert_busy(self, response):
        self.assertEqual(response.status_code, 503, response.get_json())
        self.assertEqual(response.json.get("code"), "panel_storage_busy", response.json)
        self.assertEqual(response.headers.get("Retry-After"), "1")
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        self.assertIs(response.json["ok"], False)
        self.assertIsNot(response.json.get("saved"), False)
        self.assertNotIn("secret-", response.get_data(as_text=True))

    def snapshot(self, base):
        return {str(path.relative_to(base)): path.read_bytes() for path in (base / "data").rglob("*.json")}

    def test_task_and_status_reads_check_admission_before_cold_and_warm_caches(self):
        with isolated_app() as (app, base):
            client, _ = self.prepare(app)
            restoring = self.restore_after_auth(app)
            for warm in (False, True):
                for path in ("/api/status", "/api/tasks", "/api/task/a", "/api/collections",
                             "/api/scheduler/status"):
                    with self.subTest(warm=warm, path=path):
                        if warm:
                            self.assertEqual(client.get(path).status_code, 200)
                        before = self.snapshot(base)
                        with restoring():
                            response = client.get(path)
                        self.assertEqual(self.snapshot(base), before)
                        self.assert_busy(response)
                        self.assertEqual(client.get(path).status_code, 200)

    def test_task_mutations_and_scheduler_reload_reject_before_claim_or_execution(self):
        with isolated_app() as (app, base):
            from fls_manager import models, scheduler
            from fls_manager.routes import api

            client, headers = self.prepare(app)
            models.save_collections([{"id": "c1", "name": "Collection"}])
            restoring = self.restore_after_auth(app)
            request_id = "e" * 32
            cases = [("/api/task/action/" + action + "/a", dict(request_id=request_id, **extra))
                     for action, extra in (
                         ("run", {}), ("stop", {}), ("toggle", {"enabled": False}),
                         ("pin", {"pinned": True}), ("unpin", {}), ("copy", {}),
                         ("delete", {}), ("clear_collection", {"expected_collection": ""}),
                     )]
            cases.extend(("/api/task/bulk-action", {"action": action, "task_ids": ["a"],
                                                   "request_id": request_id})
                         for action in ("enable", "disable", "run", "stop", "delete", "copy",
                                        "clear_collection", "pin", "unpin"))
            cases.extend([
                ("/api/collection/add-task/c1", {"request_id": request_id, "task_ids": ["a"],
                                                "expected_collections": {"a": ""}}),
                ("/api/scheduler/reload", {}),
            ])
            with patch.object(api, "run_task_now") as run, patch.object(api, "stop_task_now") as stop, \
                    patch.object(api, "cancel_task_retry") as cancel, \
                    patch.object(scheduler, "_apply_scheduler_plan") as apply_plan:
                for path, body in cases:
                    with self.subTest(path=path, action=body.get("action")):
                        before = self.snapshot(base)
                        with restoring():
                            response = client.post(path, json=body, headers=headers)
                        self.assertEqual(self.snapshot(base), before)
                        self.assert_busy(response)
                        self.assertFalse(scheduler.SCHEDULER_RELOAD_ACTIVE)
                for operation in (run, stop, cancel, apply_plan):
                    operation.assert_not_called()
            self.assertEqual(client.post("/api/scheduler/reload", headers=headers).status_code, 200)

    def test_completed_task_receipt_is_not_read_during_restore_and_recovers_unchanged(self):
        with isolated_app() as (app, base):
            client, headers = self.prepare(app)
            restoring = self.restore_after_auth(app)
            body = {"request_id": "e" * 32, "pinned": True}
            path = "/api/task/action/pin/a"
            original = client.post(path, json=body, headers=headers)
            self.assertEqual(original.status_code, 200, original.json)
            before = self.snapshot(base)
            with restoring():
                response = client.post(path, json=body, headers=headers)
            self.assertEqual(self.snapshot(base), before)
            self.assert_busy(response)
            self.assertEqual(client.post(path, json=body, headers=headers).json, original.json)
            self.assertEqual(self.snapshot(base), before)

    def test_notification_history_cold_and_warm_reads_preserve_busy(self):
        for populated in (False, True):
            with self.subTest(populated=populated), isolated_app() as (app, base):
                from fls_manager import notify, storage

                client, _ = self.prepare(app)
                restoring = self.restore_after_auth(app)
                if populated:
                    storage.write_json(notify.NOTIFY_HISTORY_FILE, [
                        {"id": "original", "status": "succeeded", "attempt": 1},
                    ])
                for warm in (False, True):
                    with self.subTest(warm=warm):
                        if warm:
                            self.assertEqual(client.get("/api/notify/history").status_code, 200)
                        before = self.snapshot(base)
                        with restoring():
                            response = client.get("/api/notify/history")
                        self.assertEqual(self.snapshot(base), before)
                        self.assert_busy(response)
                        self.assertEqual(client.get("/api/notify/history").status_code, 200)

    def test_authenticated_web_task_and_scheduler_paths_keep_busy_response(self):
        with isolated_app() as (app, base):
            self.prepare(app)
            restoring = self.restore_after_auth(app)
            client = app.test_client()
            with client.session_transaction() as session:
                session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                               security_verified=True, csrf_token="busy-csrf")
            for path in ("/task/delete/a", "/task/toggle/a", "/task/pin/a", "/task/collection/clear/a",
                         "/run/a", "/stop/a", "/collection/add-task/c1", "/scheduler/reload"):
                with self.subTest(path=path):
                    before = self.snapshot(base)
                    with restoring():
                        response = client.post(path, data={
                            "csrf_token": "busy-csrf", "request_id": "e" * 32, "enabled": "0",
                            "task_ids": ["a"], "expected_collections": '{"a":""}',
                        })
                    self.assertEqual(self.snapshot(base), before)
                    self.assert_busy(response)

    def test_reload_holds_data_lease_until_scheduler_plan_is_applied(self):
        with isolated_app() as (app, _):
            from fls_manager import scheduler

            client, headers = self.prepare(app)
            build_plan = scheduler._build_scheduler_plan
            apply_plan = scheduler._apply_scheduler_plan

            def assert_restore_blocked():
                body = (
                    "try:\n with directory_access(exclusive=True): print('acquired',flush=True)\n"
                    "except PanelStorageBusy: print('blocked',flush=True)\n"
                    "print('ready',flush=True)\nsys.stdin.readline()\n"
                )
                with access_tests.BackupDataAccessTests().paused_process(body) as (_, output, _):
                    output.seek(0)
                    result = output.read().replace(b"\r\n", b"\n")
                    self.assertIn(b"blocked\n", result)
                    self.assertNotIn(b"acquired\n", result)

            def build_while_restore_attempts(tasks):
                assert_restore_blocked()
                return build_plan(tasks)

            def apply_while_restore_attempts(plan):
                assert_restore_blocked()
                return apply_plan(plan)

            with patch.object(scheduler, "_build_scheduler_plan", side_effect=build_while_restore_attempts) as build, \
                    patch.object(scheduler, "_apply_scheduler_plan", side_effect=apply_while_restore_attempts) as apply:
                response = client.post("/api/scheduler/reload", headers=headers)
            build.assert_called_once()
            apply.assert_called_once()
            self.assertEqual(response.status_code, 200, response.json)
            self.assertFalse(scheduler.SCHEDULER_RELOAD_ACTIVE)
            body = "with directory_access(exclusive=True):\n print('ready',flush=True)\n sys.stdin.readline()\n"
            with access_tests.BackupDataAccessTests().paused_process(body):
                pass

    def test_invalid_task_and_notification_inputs_remain_validation_errors_while_busy(self):
        with isolated_app() as (app, base):
            client, headers = self.prepare(app)
            restoring = self.restore_after_auth(app)
            cases = (
                ("/api/task/action/pin/a", {"pinned": "yes", "request_id": "e" * 32}),
                ("/api/task/action/toggle/a", {"enabled": "yes", "request_id": "e" * 32}),
                ("/api/task/bulk-action", {"action": "invalid", "task_ids": ["a"]}),
                ("/api/collection/add-task/c1", {"task_ids": [], "request_id": "e" * 32}),
            )
            before = self.snapshot(base)
            for path, body in cases:
                with self.subTest(path=path), restoring():
                    response = client.post(path, json=body, headers=headers)
                    self.assertEqual(response.status_code, 400, response.json)
                    self.assertNotIn("code", response.json)
            with restoring():
                response = client.get("/api/notify/history?limit=invalid")
            self.assertEqual(response.status_code, 400, response.json)
            self.assertNotIn("code", response.json)
            self.assertEqual(self.snapshot(base), before)

    def test_reads_and_original_request_queries_preserve_busy_and_recover_after_unlock(self):
        with isolated_app() as (app, base):
            client, _ = self.prepare(app)
            restoring = self.restore_after_auth(app)
            for path in (
                "/api/env/variables", "/api/env/variable/ORIGINAL?reveal=1", "/api/env/snapshot?reveal=1",
                "/api/env/task-sources", "/api/env/requests/" + "a" * 32,
                "/api/config/settings", "/api/config/requests/" + "a" * 32,
                "/api/proxy/settings", "/api/proxy/settings/p", "/api/proxy/requests/" + "a" * 32,
                "/api/notify/settings", "/api/notify/settings/defaults", "/api/notify/settings/n",
                "/api/notify/requests/" + "a" * 32,
            ):
                with self.subTest(path=path):
                    before = self.snapshot(base)
                    with restoring():
                        response = client.get(path)
                    self.assertEqual(self.snapshot(base), before)
                    self.assert_busy(response)
                    self.assertEqual(client.get(path).status_code, 200)

    def test_writes_rejected_by_real_restore_lock_leave_no_changes_or_receipts(self):
        with isolated_app() as (app, base):
            client, headers = self.prepare(app)
            restoring = self.restore_after_auth(app)
            revision = client.get("/api/config/settings").json["revision"]
            cases = [
                ("/api/env/mutate", env_tests.NativeEnvApiTests().data("new"), "/api/env/requests/"),
                ("/api/proxy/settings", proxy_tests.NativeProxyApiTests().data("new"), "/api/proxy/requests/"),
                ("/api/notify/settings", notify_tests.NativeNotifyApiTests().data("new"), "/api/notify/requests/"),
                ("/api/config/settings", {"request_id": "c" * 32, "expected_revision": revision,
                                          "settings": {"theme": "editorial"}}, "/api/config/requests/"),
                ("/api/config/apply", {"request_id": "d" * 32, "expected_revision": revision}, "/api/config/requests/"),
            ]
            for path, body, query in cases:
                with self.subTest(path=path):
                    before = self.snapshot(base)
                    with restoring():
                        response = client.post(path, json=body, headers=headers)
                    self.assertEqual(self.snapshot(base), before)
                    self.assert_busy(response)
                    result = client.get(query + body["request_id"])
                    self.assertEqual(result.status_code, 200, result.json)
                    self.assertEqual(result.json["state"], "missing")

    def test_static_schema_and_input_validation_do_not_report_false_storage_busy(self):
        with isolated_app() as (app, base):
            client, headers = self.prepare(app)
            restoring = self.restore_after_auth(app)
            before = self.snapshot(base)
            with restoring():
                response = client.get("/api/notify/settings/schema")
            self.assertEqual(response.status_code, 200)
            self.assertIn("channels", response.json)
            with restoring():
                response = client.post("/api/env/mutate", json={"action": "invalid"}, headers=headers)
            self.assertEqual(response.status_code, 400)
            self.assertNotIn("code", response.json)
            self.assertEqual(self.snapshot(base), before)

    def test_busy_after_confirmed_write_preserves_saved_result_and_original_query(self):
        cases = (
            ("env", "/api/env/mutate", "/api/env/requests/"),
            ("proxy", "/api/proxy/settings", "/api/proxy/requests/"),
            ("notify", "/api/notify/settings", "/api/notify/requests/"),
            ("config", "/api/config/settings", "/api/config/requests/"),
        )
        for kind, path, query in cases:
            with self.subTest(kind=kind), isolated_app() as (app, _):
                from fls_manager import models, config
                from fls_manager.data_access import PanelStorageBusy
                from fls_manager.routes.env import native as env_native
                from fls_manager.routes.config import native as config_native

                client, headers = self.prepare(app)
                if kind == "env":
                    body = env_tests.NativeEnvApiTests().data("new")
                    target, attribute = env_native, "save_global_env"
                elif kind == "proxy":
                    body = proxy_tests.NativeProxyApiTests().data("new")
                    target, attribute = models, "save_proxies"
                elif kind == "notify":
                    body = notify_tests.NativeNotifyApiTests().data("new")
                    target, attribute = config, "save_config"
                else:
                    body = {"request_id": "c" * 32,
                            "expected_revision": client.get("/api/config/settings").json["revision"],
                            "settings": {"theme": "editorial"}}
                    target, attribute = config_native, "save_config"
                save = getattr(target, attribute)

                def saved_then_busy(*args, **kwargs):
                    save(*args, **kwargs)
                    raise PanelStorageBusy()

                with patch.object(target, attribute, side_effect=saved_then_busy) as writer:
                    result = client.post(path, json=body, headers=headers)
                writer.assert_called_once()
                self.assertIs(result.json["saved"], True, result.json)
                result = client.get(query + body["request_id"])
                self.assertEqual(result.status_code, 200, result.json)
                self.assertIs(result.json["saved"], True, result.json)
