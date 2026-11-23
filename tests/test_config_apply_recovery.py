import json
import base64
import os
import re
import threading
import time
import unittest
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from tests.test_task_save_recovery import TaskFormParser
from tests.test_ui_route_components import TOKEN, isolated_app


class ConfigApplyRecoveryTests(unittest.TestCase):
    def seed(self, base_dir):
        from fls_manager.config import save_config
        from fls_manager.models import save_tasks
        from fls_manager.scheduler import reload_scheduler

        save_config({"admin_token": TOKEN, "theme": "default"})
        save_tasks([{"id": "config-task", "name": "Config task", "command": "echo config", "cron": "0 8 * * *"}])
        reload_scheduler()
        return base_dir / "data" / "config.json"

    def form(self, client, headers=None):
        response = client.get("/config", headers=headers or {"X-Token": TOKEN})
        self.assertEqual(response.status_code, 200)
        return next(form["fields"] for form in TaskFormParser(response.get_data(as_text=True)).forms if form["action"] == "/config")

    def data(self, fields, **updates):
        data = {**fields, "admin_token": TOKEN, "theme": "editorial", "security_verify_type": "code",
                "port": "5700", "log_cleanup_minutes": "30", "log_max_size_mb": "10",
                "log_keep_per_task": "10", "task_timeout_seconds": "1800", "random_delay_seconds": "0",
                "type_py": "1", "type_sh": "1"}
        data.update(updates)
        return data

    def test_totp_qr_is_local_svg_and_never_sends_secret_to_external_service(self):
        import qrcode

        from fls_manager.security import totp_otpauth_uri

        with isolated_app() as (app, _base_dir):
            encoded_payloads = []
            original_add_data = qrcode.QRCode.add_data

            def capture_payload(qr, payload, *args, **kwargs):
                encoded_payloads.append(payload)
                return original_add_data(qr, payload, *args, **kwargs)

            with patch.object(qrcode.QRCode, "add_data", new=capture_payload):
                response = app.test_client().get("/config", headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            self.assertNotIn("api.qrserver.com", html)
            match = re.search(r'<img src="(data:image/svg\+xml;base64,[^"]+)"', html)
            self.assertIsNotNone(match)
            encoded = match.group(1).split(",", 1)[1]
            svg = base64.b64decode(encoded, validate=True)
            root = ET.fromstring(svg)
            self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg")
            paths = root.findall("{http://www.w3.org/2000/svg}path")
            self.assertEqual(len(paths), 1)
            self.assertTrue(paths[0].get("d"))
            self.assertNotIn(b"https://", svg)
            secret_match = re.search(r'name="totp_secret" value="([A-Z2-7]+)"', html)
            self.assertIsNotNone(secret_match)
            secret = secret_match.group(1)
            self.assertEqual(encoded_payloads, [totp_otpauth_uri(secret)])
            self.assertIn("otpauth://totp/", html)
            self.assertIn(secret, html)

    def test_missing_local_totp_qr_component_keeps_manual_setup_without_empty_image(self):
        with isolated_app() as (app, _base_dir), patch(
            "fls_manager.routes.config.page.totp_qr_url", return_value="",
        ):
            response = app.test_client().get("/config", headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            self.assertIn("本地二维码组件不可用", html)
            self.assertIn("otpauth://totp/", html)
            self.assertNotIn('<img src=""', html)
            self.assertNotIn("api.qrserver.com", html)

    def test_failed_save_keeps_config_session_and_draft_without_any_apply_side_effect(self):
        for ajax in (False, True):
            with self.subTest(ajax=ajax), isolated_app() as (app, base_dir):
                target = self.seed(base_dir)
                before = target.read_bytes()
                client = app.test_client()
                headers = {"X-Requested-With": "FLS-Ajax"} if ajax else {}
                with client.session_transaction() as session:
                    session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                                   security_verified=True, csrf_token="config-csrf")
                fields = self.form(client, headers)
                real_replace = Path.replace

                def replace(path, destination):
                    if destination == target:
                        raise PermissionError("config publication rejected")
                    return real_replace(path, destination)

                with patch.object(Path, "replace", new=replace), patch("fls_manager.routes.config.page.cleanup_logs") as cleanup, patch(
                    "fls_manager.routes.config.page.reload_scheduler",
                ) as reload:
                    response = client.post("/config", data=self.data(fields, admin_token="new-token"), headers=headers)
                self.assertEqual(response.status_code, 500)
                self.assertEqual(target.read_bytes(), before)
                self.assertIn("new-token", response.get_data(as_text=True))
                self.assertIn("config publication rejected", response.get_data(as_text=True))
                cleanup.assert_not_called()
                reload.assert_not_called()
                with client.session_transaction() as session:
                    self.assertEqual(session["token"], TOKEN)
                self.assertFalse((base_dir / "data" / "config_apply_state.json").exists())
                retry = next(form["fields"] for form in TaskFormParser(response.get_data(as_text=True)).forms if form["action"] == "/config")
                saved = client.post("/config", data=self.data(retry), headers=headers)
                self.assertEqual(saved.status_code, 302)

    def test_cleanup_and_reload_failures_show_saved_state_and_recover_without_resaving(self):
        for stage in ("cleanup", "reload"):
            with self.subTest(stage=stage), isolated_app() as (app, base_dir):
                from fls_manager import scheduler

                target = self.seed(base_dir)
                client = app.test_client()
                fields = self.form(client)
                patch_target = "fls_manager.routes.config.page.cleanup_logs" if stage == "cleanup" else "fls_manager.scheduler._apply_scheduler_plan"
                with patch(patch_target, side_effect=RuntimeError("application rejected")):
                    response = client.post("/config", data=self.data(fields), headers={"X-Token": TOKEN, "X-Requested-With": "FLS-Ajax"})
                self.assertEqual(response.status_code, 503)
                self.assertIn("配置已保存", response.get_data(as_text=True))
                self.assertIn('data-config-error role="alert"', response.get_data(as_text=True))
                self.assertNotIn('class="feedback feedback-error"', response.get_data(as_text=True))
                self.assertNotIn('name="admin_token"', response.get_data(as_text=True))
                self.assertEqual(json.loads(target.read_text())["theme"], "editorial")
                state_file = base_dir / "data" / "config_apply_state.json"
                self.assertEqual(json.loads(state_file.read_text())["status"], "failed")
                if stage == "cleanup":
                    self.assertEqual(scheduler.get_scheduler_state()["status"], "succeeded")
                else:
                    self.assertEqual(scheduler.get_scheduler_state()["status"], "failed")
                persisted = target.read_bytes()
                with patch(patch_target, side_effect=RuntimeError("still rejected")):
                    failed = client.post("/api/config/apply", headers={"X-Token": TOKEN})
                self.assertEqual(failed.status_code, 503)
                self.assertFalse(failed.get_json()["ok"])
                self.assertTrue(failed.get_json()["saved"])
                page = client.get("/config", headers={"X-Token": TOKEN})
                self.assertIn("still rejected", page.get_data(as_text=True))
                recovery = next(form for form in TaskFormParser(page.get_data(as_text=True)).forms if form["action"] == "/config/apply")
                for _ in range(2):
                    recovered = client.post(recovery["action"], data=recovery["fields"], headers={"X-Token": TOKEN})
                    self.assertEqual(recovered.status_code, 302)
                    self.assertEqual(target.read_bytes(), persisted)
                self.assertEqual(json.loads(state_file.read_text())["status"], "succeeded")
                self.assertEqual(scheduler.get_scheduler_state()["status"], "succeeded")

    def test_credentials_are_invalidated_before_failed_apply_and_get_retains_recovery_after_login(self):
        with isolated_app() as (app, base_dir):
            self.seed(base_dir)
            client = app.test_client()
            headers = {"X-Requested-With": "FLS-Ajax"}
            with client.session_transaction() as session:
                session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                               security_verified=True, csrf_token="config-csrf")
            fields = self.form(client, headers)
            with patch("fls_manager.routes.config.page.cleanup_logs", side_effect=RuntimeError("cleanup rejected")):
                response = client.post("/config", data=self.data(fields, security_verify_enabled="1"), headers=headers)
            self.assertEqual(response.status_code, 503)
            self.assertIn("重新登录", response.get_data(as_text=True))
            with client.session_transaction() as session:
                self.assertNotIn("token", session)
                self.assertNotIn("security_verified", session)
            self.assertEqual(client.post("/config/apply", headers=headers).status_code, 400)
            fields = self.form(client, {"X-Token": TOKEN})
            self.assertIn("config_revision", fields)
            recovered = client.post("/api/config/apply", headers={"X-Token": TOKEN})
            self.assertEqual(recovered.status_code, 200)
            self.assertEqual(recovered.get_json()["state"]["status"], "succeeded")

    def test_apply_state_write_failures_are_unknown_not_false_success(self):
        for fail_at in (1, 2):
            with self.subTest(fail_at=fail_at), isolated_app() as (app, base_dir):
                from fls_manager import storage

                target = self.seed(base_dir)
                client = app.test_client()
                fields = self.form(client)
                state_file = base_dir / "data" / "config_apply_state.json"
                real_write = storage._write_json_unlocked
                writes = 0

                def write(path, data):
                    nonlocal writes
                    if path == state_file:
                        writes += 1
                        if writes == fail_at:
                            raise OSError("apply state inaccessible")
                    return real_write(path, data)

                with patch.object(storage, "_write_json_unlocked", side_effect=write), patch("fls_manager.routes.config.page.cleanup_logs") as cleanup:
                    response = client.post("/config", data=self.data(fields), headers={"X-Token": TOKEN})
                self.assertEqual(response.status_code, 503)
                self.assertIn("apply state inaccessible", response.get_data(as_text=True))
                self.assertEqual(json.loads(target.read_text())["theme"], "editorial")
                if fail_at == 1:
                    cleanup.assert_not_called()
                else:
                    self.assertEqual(json.loads(state_file.read_text())["status"], "running")
                    page = client.get("/config", headers={"X-Token": TOKEN})
                    self.assertIn("无法确认", page.get_data(as_text=True))
                    self.assertEqual(json.loads(state_file.read_text())["status"], "unknown")
                self.assertEqual(client.post("/api/config/apply", headers={"X-Token": TOKEN}).status_code, 200)

    def test_new_login_token_takes_effect_even_when_saved_config_application_fails(self):
        with isolated_app() as (app, base_dir):
            self.seed(base_dir)
            with patch.dict(os.environ, {"FLS_TOKEN": ""}):
                client = app.test_client()
                with client.session_transaction() as session:
                    session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                                   security_verified=True, csrf_token="config-csrf")
                fields = self.form(client, {"X-Requested-With": "FLS-Ajax"})
                with patch("fls_manager.routes.config.page.reload_scheduler", side_effect=RuntimeError("scheduler unavailable")):
                    response = client.post("/config", data=self.data(fields, admin_token="replacement-token"), headers={"X-Requested-With": "FLS-Ajax"})
                self.assertEqual(response.status_code, 503)
                with client.session_transaction() as session:
                    self.assertNotIn("token", session)
                self.assertEqual(client.get("/api/scheduler/status", headers={"X-Token": TOKEN}).status_code, 401)
                page = client.get("/config", headers={"X-Token": "replacement-token"})
                self.assertEqual(page.status_code, 200)
                self.assertIn("scheduler unavailable", page.get_data(as_text=True))
                self.assertEqual(client.post("/api/config/apply", headers={"X-Token": "replacement-token"}).status_code, 200)

    def test_invalid_selection_and_totp_have_zero_persistence_or_application_effects(self):
        with isolated_app() as (app, base_dir):
            target = self.seed(base_dir)
            before = target.read_bytes()
            client = app.test_client()
            fields = self.form(client)
            invalid = ({"theme": "invalid"}, {"security_verify_type": "invalid"}, {"port": "0"},
                       {"type_py": "invalid"}, {"security_verify_enabled": "invalid"}, {"online_script_source": "bad\x00source"},
                       {"random_delay_seconds": "121"}, {"log_keep_per_task": "invalid"},
                       {"security_verify_enabled": "1", "security_verify_type": "totp", "totp_code": "invalid"},
                       {"security_verify_enabled": "1", "security_verify_type": "totp", "totp_secret": "!invalid!", "totp_code": "123456"})
            for selection in invalid:
                with self.subTest(selection=selection), patch("fls_manager.routes.config.page.cleanup_logs") as cleanup, patch(
                    "fls_manager.routes.config.page.reload_scheduler",
                ) as reload:
                    response = client.post("/config", data=self.data(fields, **selection), headers={"X-Token": TOKEN})
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(target.read_bytes(), before)
                    self.assertFalse((base_dir / "data" / "config_apply_state.json").exists())
                    cleanup.assert_not_called()
                    reload.assert_not_called()

    def test_revision_conflict_preserves_draft_and_current_config_and_missing_revision_is_rejected(self):
        with isolated_app() as (app, base_dir):
            target = self.seed(base_dir)
            client = app.test_client()
            fields = self.form(client)
            self.assertEqual(client.post("/config", data=self.data(fields), headers={"X-Token": TOKEN}).status_code, 302)
            before = target.read_bytes()
            response = client.post("/config", data=self.data(fields, theme="glassmorphism"), headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 409)
            html = response.get_data(as_text=True)
            self.assertIn('value="glassmorphism" selected', html)
            self.assertIn('aria-label="当前配置"', html)
            self.assertEqual(target.read_bytes(), before)
            missing = client.post("/config", data=self.data({}), headers={"X-Token": TOKEN})
            self.assertEqual(missing.status_code, 409)
            retry = next(form["fields"] for form in TaskFormParser(html).forms if form["action"] == "/config")
            self.assertEqual(client.post("/config", data=self.data(retry, theme="glassmorphism"), headers={"X-Token": TOKEN}).status_code, 302)

    def test_deepseek_and_neumorphism_theme_selections_persist_and_render(self):
        with isolated_app() as (app, base_dir):
            self.seed(base_dir)
            client = app.test_client()
            for theme in ("deepseek", "neumorphism"):
                with self.subTest(theme=theme):
                    fields = self.form(client)
                    with patch("fls_manager.routes.config.page.cleanup_logs"), patch(
                        "fls_manager.routes.config.page.reload_scheduler",
                    ):
                        response = client.post("/config", data=self.data(fields, theme=theme), headers={"X-Token": TOKEN})
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(json.loads((base_dir / "data" / "config.json").read_text())["theme"], theme)
                    page = client.get("/", headers={"X-Token": TOKEN})
                    self.assertIn(f"fls-theme-{theme}", page.get_data(as_text=True))

    def test_corrupt_configuration_is_not_replaced_by_defaults(self):
        for corrupted in (b"{broken", b"\xff", b"[]"):
            with self.subTest(corrupted=corrupted), isolated_app() as (app, base_dir):
                target = self.seed(base_dir)
                target.write_bytes(corrupted)
                client = app.test_client()
                self.assertEqual(client.get("/config", headers={"X-Token": TOKEN}).status_code, 500)
                response = client.post("/config", data={"admin_token": "draft-token"}, headers={"X-Token": TOKEN})
                self.assertEqual(response.status_code, 500)
                self.assertIn("draft-token", response.get_data(as_text=True))
                self.assertEqual(target.read_bytes(), corrupted)
                failed = client.post("/api/config/apply", headers={"X-Token": TOKEN})
                self.assertEqual(failed.status_code, 500)
                self.assertIsNone(failed.get_json()["saved"])

    def test_concurrent_configuration_forms_publish_once_and_conflict_without_apply(self):
        with isolated_app() as (app, base_dir):
            target = self.seed(base_dir)
            fields = self.form(app.test_client())
            barrier = threading.Barrier(2)

            def save(theme):
                barrier.wait(timeout=3)
                return app.test_client().post("/config", data=self.data(fields, theme=theme), headers={"X-Token": TOKEN})

            with patch("fls_manager.routes.config.page.cleanup_logs") as cleanup, ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(save, theme) for theme in ("editorial", "glassmorphism")]
                responses = [future.result(timeout=5) for future in futures]
            self.assertEqual(sorted(response.status_code for response in responses), [302, 409])
            cleanup.assert_called_once()
            winning_theme = "editorial" if responses[0].status_code == 302 else "glassmorphism"
            self.assertEqual(json.loads(target.read_text())["theme"], winning_theme)

    def test_unrelated_notification_updates_do_not_expire_config_form_or_get_overwritten(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.config import save_config

            target = self.seed(base_dir)
            client = app.test_client()
            fields = self.form(client)
            save_config({"notify_items": [{"id": "concurrent", "channel": "bark", "enabled": True, "config": {}}]})
            response = client.post("/config", data=self.data(fields), headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 302)
            cfg = json.loads(target.read_text())
            self.assertEqual(cfg["theme"], "editorial")
            self.assertEqual(cfg["notify_items"][0]["id"], "concurrent")

    def test_post_save_read_failure_reports_saved_and_recovery_uses_persisted_config(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.routes.config import page

            target = self.seed(base_dir)
            client = app.test_client()
            fields = self.form(client)
            snapshot = page._config_snapshot
            calls = 0

            def read():
                nonlocal calls
                calls += 1
                if calls == 3:
                    raise PermissionError("post-save config inaccessible")
                return snapshot()

            with patch.object(page, "_config_snapshot", side_effect=read), patch.object(page, "cleanup_logs") as cleanup:
                response = client.post("/config", data=self.data(fields), headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 503)
            self.assertIn("配置已保存", response.get_data(as_text=True))
            self.assertIn("post-save config inaccessible", response.get_data(as_text=True))
            self.assertEqual(json.loads(target.read_text())["theme"], "editorial")
            cleanup.assert_not_called()
            page_response = client.get("/config", headers={"X-Token": TOKEN})
            self.assertIn("应用待恢复", page_response.get_data(as_text=True))
            before = target.read_bytes()
            self.assertEqual(client.post("/api/config/apply", headers={"X-Token": TOKEN}).status_code, 200)
            self.assertEqual(target.read_bytes(), before)

    def test_csrf_expiry_forbidden_and_get_reapply_have_no_side_effects(self):
        with isolated_app() as (app, base_dir):
            target = self.seed(base_dir)
            before = target.read_bytes()
            client = app.test_client()
            headers = {"X-Requested-With": "FLS-Ajax"}
            with patch("fls_manager.routes.config.page._apply_saved_config") as apply:
                for url in ("/config", "/config/apply", "/api/config/apply"):
                    with self.subTest(url=url):
                        with client.session_transaction() as session:
                            session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600, security_verified=True, csrf_token="config-csrf")
                        self.assertEqual(client.post(url, headers=headers).status_code, 400)
                        with client.session_transaction() as session:
                            session["token_expire_at"] = 1
                        self.assertEqual(client.post(url, data={"csrf_token": "config-csrf"}, headers=headers).status_code, 401)
                        with client.session_transaction() as session:
                            session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600, security_verified=True, csrf_token="config-csrf")
                        with patch("fls_manager.auth.query_token_enabled", return_value=False):
                            self.assertEqual(client.post(url, query_string={"token": TOKEN}, data={"csrf_token": "config-csrf"}, headers=headers).status_code, 403)
                self.assertEqual(client.get("/config/apply", headers={"X-Token": TOKEN}).status_code, 405)
                apply.assert_not_called()
            self.assertEqual(target.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
