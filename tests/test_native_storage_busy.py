import contextlib
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
